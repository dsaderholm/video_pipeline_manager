import sqlite3
import json
import time
import os
from app import logger
from app.core.utils import execute_curl, get_latest_video, cleanup_video, format_upload_command
from app.core.email_utils import send_task_completion_notification

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

def process_video_pipeline(task_id, preview_mode=False, dry_run=False):
    """Main pipeline process for generating, processing, and uploading videos
    
    Args:
        task_id (int): ID of the task to process
        preview_mode (bool): If True, generate and process video but don't upload
        dry_run (bool): If True, simulate all steps without executing
        
    Returns:
        - If preview_mode: path to the generated video file
        - If dry_run: bool indicating success
        - Otherwise: None
    """
    # For dry runs, we don't need to acquire a lock
    if not dry_run and not check_and_set_lock():
        logger.info(f"Another task is currently running. Task {task_id} will retry later.")
        return False if dry_run else None
    
    try:
        with sqlite3.connect('pipeline.db') as conn:
            c = conn.cursor()
            
            if not dry_run:
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
                    logger.info(f"[DRY RUN] Would execute generator: {task_data[-1]}")
                else:
                    success, stdout, stderr = execute_curl(task_data[-1])
                    if not success:
                        raise Exception(f"Generator failed: {stderr}")

                    video_file = get_latest_video()
                    if not video_file:
                        raise Exception("No video file was generated")
                
                # Apply utilities in sequence
                if task_data[3]:  # utilities JSON string
                    utilities = json.loads(task_data[3])
                    logger.info(f"Task {task_id}: {'[DRY RUN] Would process' if dry_run else 'Processing'} {len(utilities)} utilities")
                    
                    for index, util_id in enumerate(utilities, 1):
                        c.execute("SELECT id, name, utility_curl FROM utilities WHERE id=?", (util_id,))
                        util = c.fetchone()
                        if not util:
                            logger.warning(f"Task {task_id}: Utility {util_id} not found, skipping")
                            continue
                        
                        if dry_run:
                            logger.info(f"[DRY RUN] Would run utility {index}/{len(utilities)} - {util[1]}")
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
                    preview_dir = os.path.join(os.path.dirname(video_file), 'previews')
                    os.makedirs(preview_dir, exist_ok=True)
                    preview_path = os.path.join(preview_dir, f'preview_task_{task_id}.mp4')
                    os.rename(video_file, preview_path)
                    return preview_path

                # For dry run or preview mode, skip uploads
                if dry_run or preview_mode:
                    if dry_run:
                        logger.info("[DRY RUN] Would upload to platforms:", task_data[5])
                    return True if dry_run else None

                # Upload to platforms
                platforms = json.loads(task_data[5])
                for platform_id in platforms:
                    c.execute("SELECT * FROM platform_accounts WHERE id=?", (platform_id,))
                    platform = c.fetchone()
                    if not platform:
                        continue

                    # Format upload data
                    platform_data = {
                        'account_name': platform[2],
                        'default_hashtags': platform[5]
                    }
                    task_upload_data = {
                        'sound_name': task_data[7],
                        'sound_volume': task_data[8],
                        'hashtags': task_data[6]
                    }

                    # Try primary upload
                    upload_cmd = format_upload_command(platform[3], video_file, task_upload_data, platform_data)
                    success, stdout, stderr = execute_curl(upload_cmd)
                    last_error = stderr
                    
                    # Try first fallback if available and primary failed
                    if not success and platform[4]:
                        logger.info(f"Primary upload failed for platform {platform[1]}, trying first fallback")
                        fallback_cmd = format_upload_command(platform[4], video_file, task_upload_data, platform_data)
                        success, stdout, stderr = execute_curl(fallback_cmd)
                        last_error = stderr

                    # Try second fallback if available and first fallback failed
                    if not success and platform[5]:  # Index 5 is fallback_curl_2
                        logger.info(f"First fallback failed for platform {platform[1]}, trying second fallback")
                        fallback_cmd_2 = format_upload_command(platform[5], video_file, task_upload_data, platform_data)
                        success, stdout, stderr = execute_curl(fallback_cmd_2)
                        last_error = stderr
                    
                    if not success:
                        raise Exception(f"Upload failed for platform {platform[1]}: {last_error}")

                # Update task status for real runs
                if not dry_run:
                    c.execute("UPDATE tasks SET status=? WHERE id=?", ('completed', task_id))
                    conn.commit()
                    
                    logger.info(f"Task {task_id} completed successfully")
                    
                    # Send success notification if email is configured
                    if task_data[10]:  # email_notify field
                        send_task_completion_notification(
                            task_id=task_id,
                            task_name=task_data[1],  # name field
                            to_email=task_data[10],   # email_notify field
                            success=True
                        )
                
                return True if dry_run else None
                
            except Exception as e:
                error_msg = f"Pipeline error for task {task_id}: {str(e)}"
                logger.error(error_msg)
                
                if not dry_run:
                    # Update task status
                    c.execute("UPDATE tasks SET status=? WHERE id=?", ('failed', task_id))
                    conn.commit()
                    
                    # Send failure notification if email is configured
                    if task_data[10]:  # email_notify field
                        send_task_completion_notification(
                            task_id=task_id,
                            task_name=task_data[1],  # name field
                            to_email=task_data[10],   # email_notify field
                            success=False
                        )
                
                return False if dry_run else None
                
            finally:
                # Clean up video file unless it's a preview that we want to keep
                if video_file and not preview_mode:
                    cleanup_video(video_file)
                    
    finally:
        # Always release the lock for real runs
        if not dry_run:
            release_lock()