import sqlite3
import json
import time
import os
import logging
import shutil
from app import logger
from app.core.utils import execute_curl, get_latest_video, cleanup_video, format_upload_command
from app.core.email_utils import send_task_completion_notification
from flask import current_app

def create_logger_with_task_id(task_id):
    """Create a logger that attaches task_id to all records"""
    logger_instance = logging.getLogger(f'task_{task_id}')
    
    # Add a filter to inject task_id into the record
    class TaskFilter(logging.Filter):
        def filter(self, record):
            record.task_id = task_id
            return True
    
    logger_instance.addFilter(TaskFilter())
    return logger_instance

def check_and_set_lock():
    """Check if any task is running and set lock if not"""
    with sqlite3.connect('pipeline.db') as conn:
        c = conn.cursor()
        # First check if the lock table exists
        c.execute("""
            CREATE TABLE IF NOT EXISTS task_lock (
                id INTEGER PRIMARY KEY,
                locked INTEGER DEFAULT 0,
                task_id INTEGER,
                locked_at TIMESTAMP
            )
        """)
        conn.commit()
        
        # Insert initial lock record if it doesn't exist
        c.execute("INSERT OR IGNORE INTO task_lock (id, locked) VALUES (1, 0)")
        conn.commit()
        
        # Check lock with timeout cleanup (clear locks older than 30 minutes)
        c.execute("""
            UPDATE task_lock 
            SET locked = 0, task_id = NULL, locked_at = NULL 
            WHERE locked = 1 
            AND datetime(locked_at, '+30 minutes') < datetime('now')
        """)
        
        # Try to acquire lock
        c.execute("""
            UPDATE task_lock 
            SET locked = 1, task_id = NULL, locked_at = datetime('now')
            WHERE id = 1 AND locked = 0
        """)
        conn.commit()
        
        # Check if we got the lock
        return c.rowcount > 0

def release_lock():
    """Release the task lock"""
    with sqlite3.connect('pipeline.db') as conn:
        c = conn.cursor()
        c.execute("""
            UPDATE task_lock 
            SET locked = 0, task_id = NULL, locked_at = NULL 
            WHERE id = 1
        """)
        conn.commit()

def get_preview_dir():
    """Get the absolute path to the previews directory"""
    webapp_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    preview_dir = os.path.join(webapp_root, 'static', 'previews')
    os.makedirs(preview_dir, exist_ok=True)  # Ensure directory exists
    return preview_dir

def process_video_pipeline(task_id, preview_mode=False, dry_run=False):
    """Main pipeline process for generating, processing, and uploading videos"""
    
    # Create a task-specific logger
    task_logger = create_logger_with_task_id(task_id)
    
    # Add mode-specific logging at the start
    mode = "dry run" if dry_run else "preview" if preview_mode else "normal"
    task_logger.info(f"Starting task in {mode} mode")
    
    current_video_file = None
    safe_video_path = None
    
    # For dry runs or previews, we don't need to acquire a lock
    if not (dry_run or preview_mode) and not check_and_set_lock():
        task_logger.info("Another task is currently running. Task will retry later.")
        return False if dry_run else None
    
    try:
        with sqlite3.connect('pipeline.db') as conn:
            c = conn.cursor()
            
            if not (dry_run or preview_mode):
                # Update lock with current task_id
                c.execute("UPDATE task_lock SET task_id = ? WHERE id = 1", (task_id,))
                conn.commit()
            
            # Get task information with explicit column selection
            c.execute("""
                SELECT t.id, t.name, t.generator_id, t.utilities, t.schedule, 
                       t.hashtags, t.sound_name, t.sound_volume, t.status, 
                       t.email_notify, t.retry_count, t.created_at,
                       g.generator_curl 
                FROM tasks t
                JOIN generators g ON t.generator_id = g.id
                WHERE t.id=?
            """, (task_id,))
            task_data = c.fetchone()
            
            if not task_data:
                task_logger.error("No task found with this ID")
                return False if dry_run else None

            # Convert task_data tuple to dict for format_upload_command
            task_dict = {
                'id': task_data[0],
                'name': task_data[1],
                'hashtags': task_data[5],
                'sound_name': task_data[6],
                'sound_volume': task_data[7]
            }

            try:
                # Generate initial video
                if dry_run:
                    task_logger.info(f"[DRY RUN] Would execute generator: {task_data[-1]}")
                else:
                    task_logger.info("Executing generator...")
                    # Added video validation for generator output
                    success, stdout, stderr = execute_curl(task_data[-1], validate_output=True)
                    if not success:
                        error_msg = f"Generator failed: {stderr}"
                        task_logger.error(error_msg)
                        raise Exception(error_msg)
                    task_logger.info("Generator completed successfully")

                    current_video_file = get_latest_video()
                    if not current_video_file:
                        error_msg = "No video file was generated"
                        task_logger.error(error_msg)
                        raise Exception(error_msg)
                    task_logger.info(f"Video file generated: {current_video_file}")
                
                # Apply utilities in sequence
                if task_data[3]:  # utilities JSON string
                    utilities = json.loads(task_data[3])
                    step_msg = f"{'[DRY RUN] Would process' if dry_run else 'Processing'} {len(utilities)} utilities"
                    task_logger.info(step_msg)
                    
                    for index, util_id in enumerate(utilities, 1):
                        c.execute("SELECT id, name, utility_curl FROM utilities WHERE id=?", (util_id,))
                        util = c.fetchone()
                        if not util:
                            task_logger.warning(f"Utility {util_id} not found, skipping")
                            continue
                        
                        if dry_run:
                            task_logger.info(f"[DRY RUN] Would run utility {index}/{len(utilities)} - {util[1]}")
                            continue
                            
                        task_logger.info(f"Running utility {index}/{len(utilities)} - {util[1]}")
                        
                        # Apply the utility using the current video file path
                        util_cmd = util[2].format(input=current_video_file)
                        # Added video validation for utility output
                        success, stdout, stderr = execute_curl(util_cmd, validate_output=True)
                        if not success:
                            error_msg = f"Utility {util[1]} ({index}/{len(utilities)}) failed: {stderr}"
                            task_logger.error(error_msg)
                            raise Exception(error_msg)
                            
                        task_logger.info(f"Completed utility {index}/{len(utilities)} - {util[1]}")

                # For preview mode, save the processed video and return its path
                if preview_mode and current_video_file:
                    preview_dir = get_preview_dir()
                    preview_path = os.path.join(preview_dir, f'preview_task_{task_id}.mp4')
                    if os.path.exists(preview_path):
                        task_logger.info("Removing existing preview file")
                        os.remove(preview_path)
                    task_logger.info(f"Saving preview to {preview_path}")
                    shutil.copy2(current_video_file, preview_path)
                    task_logger.info("Preview saved successfully")
                    c.execute("UPDATE tasks SET status='completed' WHERE id=?", (task_id,))
                    conn.commit()
                    return preview_path

                # For dry run mode, simulate platform uploads
                if dry_run:
                    c.execute("""
                        SELECT p.name, tpa.account_name
                        FROM task_platform_accounts tpa
                        JOIN platforms p ON tpa.platform_id = p.id
                        WHERE tpa.task_id = ?
                    """, (task_id,))
                    platform_data = c.fetchall()
                    if platform_data:
                        task_logger.info(f"[DRY RUN] Would upload to {len(platform_data)} platforms")
                        for platform_name, account_name in platform_data:
                            task_logger.info(f"[DRY RUN] Would upload to {platform_name} with account {account_name}")
                    return True

                # For preview mode, skip uploads
                if preview_mode:
                    return None

                task_logger.info(f"Video file path before upload: {current_video_file}")

                # Process uploads using the new schema
                c.execute("""
                    SELECT p.id, p.name, p.uploader_curl, tpa.account_name, p.default_hashtags
                    FROM task_platform_accounts tpa
                    JOIN platforms p ON tpa.platform_id = p.id
                    WHERE tpa.task_id = ?
                """, (task_id,))
                platform_data_list = c.fetchall()

                if platform_data_list:
                    task_logger.info(f"Processing {len(platform_data_list)} platform uploads")
                    
                    for platform_data in platform_data_list:
                        platform_id, platform_name, upload_curl, account_name, default_hashtags = platform_data
                        task_logger.info(f"Uploading to {platform_name} with account {account_name}")
                        
                        # Create platform_dict for format_upload_command
                        platform_dict = {
                            'account_name': account_name,
                            'default_hashtags': default_hashtags
                        }
                        
                        # Format the upload command with all required parameters
                        # This now returns both the command and the safe file path
                        upload_cmd, safe_video_path = format_upload_command(
                            upload_curl,
                            current_video_file,
                            task_dict,
                            platform_dict
                        )
                        
                        if not upload_cmd or not safe_video_path:
                            raise Exception("Failed to prepare upload command or safe video path")
                        
                        # No video validation for uploads - they handle their own validation
                        success, stdout, stderr = execute_curl(upload_cmd)
                        
                        # Restore original filename after upload attempt
                        try:
                            if os.path.exists(safe_video_path):
                                os.rename(safe_video_path, current_video_file)
                        except OSError as e:
                            task_logger.error(f"Failed to restore original filename: {e}")
                        
                        if not success:
                            error_msg = f"Upload to {platform_name} failed: {stderr}"
                            task_logger.error(error_msg)
                            raise Exception(error_msg)
                            
                        task_logger.info(f"Successfully uploaded to {platform_name}")
                
                # Update task status and send notification
                c.execute("UPDATE tasks SET status='completed' WHERE id=?", (task_id,))
                conn.commit()
                
                if task_data[9]:  # If email notifications are enabled
                    c.execute("SELECT id, name, email_notify FROM tasks WHERE id=?", (task_id,))
                    task_info = c.fetchone()
                    if task_info and task_info[2]:  # email_notify contains the email address
                        send_task_completion_notification(task_id, task_info[1], task_info[2], success=True)
                    task_logger.info("Sent completion notification")
                
                # Clean up video file unless it's a preview that we want to keep
                if current_video_file and not preview_mode:
                    cleanup_video(current_video_file)
                    if safe_video_path and os.path.exists(safe_video_path):
                        cleanup_video(safe_video_path)
                    task_logger.info("Cleaned up temporary video files")
                
                return True

            except Exception as e:
                error_msg = f"Pipeline error: {str(e)}"
                task_logger.error(error_msg)
                # Clean up any remaining safe video path if exists
                if safe_video_path and os.path.exists(safe_video_path):
                    try:
                        os.rename(safe_video_path, current_video_file)
                    except OSError:
                        pass
                raise
                
            finally:
                # Always try to restore original filename if safe path exists
                if safe_video_path and os.path.exists(safe_video_path):
                    try:
                        os.rename(safe_video_path, current_video_file)
                    except OSError:
                        pass
    
    except Exception as e:
        error_msg = f"Task failed in {mode} mode: {str(e)}"
        task_logger.error(error_msg)
        
        # Send failure notification if email notifications are enabled
        if not (dry_run or preview_mode):
            try:
                c.execute("SELECT id, name, email_notify FROM tasks WHERE id=?", (task_id,))
                task_info = c.fetchone()
                if task_info and task_info[2]:
                    send_task_completion_notification(task_id, task_info[1], task_info[2], success=False)
            except Exception as notify_error:
                task_logger.error(f"Failed to send error notification: {str(notify_error)}")
        
        raise
        
    finally:
        # Always release the lock for real runs
        if not (dry_run or preview_mode):
            release_lock()
            task_logger.info("Released pipeline lock")
        
        task_logger.info(f"Completed {mode} mode")