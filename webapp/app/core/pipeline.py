import sqlite3
import json
import time
import os
from app import logger
from app.core.utils import execute_curl, get_latest_video, cleanup_video, format_upload_command
from app.core.email_utils import send_task_completion_notification
from flask import current_app

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
    
    # Add mode-specific logging at the start
    mode = "dry run" if dry_run else "preview" if preview_mode else "normal"
    logger.info(f"Starting task {task_id} in {mode} mode")
    
    # For dry runs or previews, we don't need to acquire a lock
    if not (dry_run or preview_mode) and not check_and_set_lock():
        logger.info(f"Another task is currently running. Task {task_id} will retry later.")
        return False if dry_run else None
    
    try:
        with sqlite3.connect('pipeline.db') as conn:
            c = conn.cursor()
            
            if not (dry_run or preview_mode):
                # Update lock with current task_id
                c.execute("UPDATE task_lock SET task_id = ? WHERE id = 1", (task_id,))
                conn.commit()
            
            # Get task information
            c.execute("""
                SELECT t.*, g.generator_curl 
                FROM tasks t
                JOIN generators g ON t.generator_id = g.id
                WHERE t.id=?
            """, (task_id,))
            task_data = c.fetchone()
            
            if not task_data:
                logger.error(f"No task found with ID {task_id}")
                return False if dry_run else None

            video_file = None
            preview_path = None
            try:
                # Generate initial video
                if dry_run:
                    logger.info(f"[DRY RUN] Task {task_id}: Would execute generator: {task_data[-1]}")
                else:
                    logger.info(f"Task {task_id}: Executing generator...")
                    success, stdout, stderr = execute_curl(task_data[-1])
                    if not success:
                        error_msg = f"Generator failed: {stderr}"
                        logger.error(f"Task {task_id}: {error_msg}")
                        raise Exception(error_msg)
                    logger.info(f"Task {task_id}: Generator completed successfully")

                    video_file = get_latest_video()
                    if not video_file:
                        error_msg = "No video file was generated"
                        logger.error(f"Task {task_id}: {error_msg}")
                        raise Exception(error_msg)
                    logger.info(f"Task {task_id}: Video file generated: {video_file}")
                
                # Apply utilities in sequence
                if task_data[3]:  # utilities JSON string
                    utilities = json.loads(task_data[3])
                    step_msg = f"Task {task_id}: {'[DRY RUN] Would process' if dry_run else 'Processing'} {len(utilities)} utilities"
                    logger.info(step_msg)
                    
                    for index, util_id in enumerate(utilities, 1):
                        c.execute("SELECT id, name, utility_curl FROM utilities WHERE id=?", (util_id,))
                        util = c.fetchone()
                        if not util:
                            logger.warning(f"Task {task_id}: Utility {util_id} not found, skipping")
                            continue
                        
                        if dry_run:
                            logger.info(f"[DRY RUN] Task {task_id}: Would run utility {index}/{len(utilities)} - {util[1]}")
                            continue
                            
                        logger.info(f"Task {task_id}: Running utility {index}/{len(utilities)} - {util[1]}")
                        
                        # The utility will overwrite the input file
                        util_cmd = util[2].format(input=video_file)
                        success, stdout, stderr = execute_curl(util_cmd)
                        if not success:
                            error_msg = f"Utility {util[1]} ({index}/{len(utilities)}) failed: {stderr}"
                            logger.error(f"Task {task_id}: {error_msg}")
                            raise Exception(error_msg)
                            
                        logger.info(f"Task {task_id}: Completed utility {index}/{len(utilities)} - {util[1]}")

                # For preview mode, save the processed video and return its path
                if preview_mode and video_file:
                    preview_dir = get_preview_dir()
                    preview_path = os.path.join(preview_dir, f'preview_task_{task_id}.mp4')
                    if os.path.exists(preview_path):
                        logger.info(f"Task {task_id}: Removing existing preview file")
                        os.remove(preview_path)
                    logger.info(f"Task {task_id}: Saving preview to {preview_path}")
                    os.rename(video_file, preview_path)
                    logger.info(f"Task {task_id}: Preview saved successfully")
                    c.execute("UPDATE tasks SET status='completed' WHERE id=?", (task_id,))
                    conn.commit()
                    return preview_path

                # For dry run or preview mode, skip uploads
                if dry_run or preview_mode:
                    if dry_run:
                        platforms = json.loads(task_data[5])
                        logger.info(f"[DRY RUN] Task {task_id}: Would upload to {len(platforms)} platforms")
                        for platform_id in platforms:
                            c.execute("SELECT platform FROM platform_accounts WHERE id=?", (platform_id,))
                            platform = c.fetchone()
                            if platform:
                                logger.info(f"[DRY RUN] Task {task_id}: Would upload to platform: {platform[0]}")
                    return True if dry_run else None

                # Process uploads
                if task_data[5]:  # platform_accounts JSON string
                    platforms = json.loads(task_data[5])
                    logger.info(f"Task {task_id}: Processing {len(platforms)} platform uploads")
                    
                    for platform_id in platforms:
                        c.execute("""
                            SELECT pa.platform, pa.upload_curl, pa.credentials
                            FROM platform_accounts pa
                            WHERE pa.id=?
                        """, (platform_id,))
                        platform_data = c.fetchone()
                        
                        if not platform_data:
                            logger.warning(f"Task {task_id}: Platform account {platform_id} not found, skipping")
                            continue
                            
                        platform, upload_curl, credentials = platform_data
                        logger.info(f"Task {task_id}: Uploading to {platform}")
                        
                        # Format the upload command with credentials and video file
                        upload_cmd = format_upload_command(upload_curl, credentials, video_file)
                        success, stdout, stderr = execute_curl(upload_cmd)
                        
                        if not success:
                            error_msg = f"Upload to {platform} failed: {stderr}"
                            logger.error(f"Task {task_id}: {error_msg}")
                            raise Exception(error_msg)
                            
                        logger.info(f"Task {task_id}: Successfully uploaded to {platform}")
                
                # Update task status and send notification
                c.execute("UPDATE tasks SET status='completed' WHERE id=?", (task_id,))
                conn.commit()
                
                if task_data[4]:  # notification_email
                    send_task_completion_notification(task_data[4], task_id)
                    logger.info(f"Task {task_id}: Sent completion notification")
                
                return True

            except Exception as e:
                error_msg = f"Pipeline error for task {task_id}: {str(e)}"
                logger.error(error_msg)
                raise
                
            finally:
                # Clean up video file unless it's a preview that we want to keep
                if video_file and not preview_mode:
                    cleanup_video(video_file)
                    logger.info(f"Task {task_id}: Cleaned up temporary video file")
    
    except Exception as e:
        logger.error(f"Task {task_id} failed in {mode} mode: {str(e)}")
        raise
        
    finally:
        # Always release the lock for real runs
        if not (dry_run or preview_mode):
            release_lock()
            logger.info(f"Task {task_id}: Released pipeline lock")
        
        logger.info(f"Task {task_id}: Completed {mode} mode")