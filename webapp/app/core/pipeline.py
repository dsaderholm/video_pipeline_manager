import sqlite3
import json
import time
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

def process_video_pipeline(task_id):
    """Main pipeline process for generating, processing, and uploading videos"""
    # Try to acquire lock, exit if another task is running
    if not check_and_set_lock():
        logger.info(f"Another task is currently running. Task {task_id} will retry later.")
        return
    
    try:
        with sqlite3.connect('pipeline.db') as conn:
            c = conn.cursor()
            
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
                return

            video_file = None
            try:
                # Generate initial video
                success, stdout, stderr = execute_curl(task_data[-1])  # generator_curl is the last column
                if not success:
                    raise Exception(f"Generator failed: {stderr}")

                video_file = get_latest_video()
                if not video_file:
                    raise Exception("No video file was generated")
                
                # Apply utilities in sequence
                if task_data[3]:  # utilities JSON string
                    utilities = json.loads(task_data[3])
                    logger.info(f"Task {task_id}: Processing {len(utilities)} utilities in order: {utilities}")
                    
                    for index, util_id in enumerate(utilities, 1):
                        c.execute("SELECT id, name, utility_curl FROM utilities WHERE id=?", (util_id,))
                        util = c.fetchone()
                        if not util:
                            logger.warning(f"Task {task_id}: Utility {util_id} not found, skipping")
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

                # Update task status
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
                
            except Exception as e:
                error_msg = f"Pipeline error for task {task_id}: {str(e)}"
                logger.error(error_msg)
                
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
                
            finally:
                if video_file:
                    cleanup_video(video_file)
                    
    finally:
        # Always release the lock, even if an exception occurs
        release_lock()