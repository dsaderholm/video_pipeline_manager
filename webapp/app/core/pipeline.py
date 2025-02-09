import sqlite3
import json
import time
import os
import sys
import logging
import shutil
import threading
from datetime import datetime
from app import logger
from app.core.utils import execute_curl, get_latest_video, cleanup_video, format_upload_command, log_with_details
from app.core.email_utils import send_task_completion_notification
from flask import current_app

# Global lock for database operations
db_lock = threading.Lock()

def get_db_path():
    return os.path.join('webapp', 'database', 'pipeline.db')

def get_db_connection(timeout=60.0):
    """Get a database connection with timeout and proper settings"""
    conn = sqlite3.connect(get_db_path(), timeout=timeout)
    conn.execute("PRAGMA journal_mode=WAL")  # Write-Ahead Logging for better concurrency
    conn.execute("PRAGMA busy_timeout=60000")  # 60 second timeout
    conn.execute("PRAGMA synchronous=NORMAL")  # Slightly less durability for better concurrency
    return conn

def log_with_task_details(level, message, task_id, details=None):
    """Helper function to log with task ID and structured details"""
    if details is None:
        details = {}
    details['task_id'] = task_id
    
    # Use a separate connection for logging to avoid deadlocks
    try:
        with get_db_connection() as log_conn:
            log_with_details(level, message, task_id=task_id, details=details, source='pipeline')
    except sqlite3.OperationalError as e:
        # If logging fails, write to stderr as fallback
        print(f"ERROR: Failed to log to database: {str(e)}", file=sys.stderr)
        print(f"{level}: {message} (Task {task_id})", file=sys.stderr)

def create_logger_with_task_id(task_id):
    """Create a logger that attaches task_id to all records"""
    logger_instance = logging.getLogger(f'task_{task_id}')
    
    class TaskFilter(logging.Filter):
        def filter(self, record):
            record.task_id = task_id
            return True
    
    logger_instance.addFilter(TaskFilter())
    return logger_instance

def check_and_set_lock():
    """Check if any task is running and set lock if not"""
    lock_details = {
        'operation': 'check_and_set',
        'timestamp': datetime.now().isoformat()
    }
    
    with db_lock:  # Use thread lock for atomic operation
        try:
            conn = get_db_connection()
            with conn:
                c = conn.cursor()
                
                # Create lock table if it doesn't exist
                c.execute("""
                    CREATE TABLE IF NOT EXISTS task_lock (
                        id INTEGER PRIMARY KEY,
                        locked INTEGER DEFAULT 0,
                        task_id INTEGER,
                        locked_at TIMESTAMP
                    )
                """)
                
                # Insert default lock record if not exists
                c.execute("INSERT OR IGNORE INTO task_lock (id, locked) VALUES (1, 0)")
                
                # Clear stale locks (older than 30 minutes)
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
                
                acquired = c.rowcount > 0
                lock_details['acquired'] = acquired
                
                log_with_task_details('INFO', 
                    "Successfully acquired pipeline lock" if acquired else "Failed to acquire pipeline lock",
                    task_id=None,
                    details=lock_details)
                
                return acquired
                
        except Exception as e:
            lock_details['error'] = str(e)
            log_with_task_details('ERROR', f"Error managing pipeline lock", 
                task_id=None,
                details=lock_details)
            raise

def release_lock():
    """Release the task lock with improved error handling"""
    with db_lock:
        try:
            conn = get_db_connection()
            with conn:
                c = conn.cursor()
                c.execute("""
                    UPDATE task_lock 
                    SET locked = 0, task_id = NULL, locked_at = NULL 
                    WHERE id = 1
                """)
        except Exception as e:
            # Log error but don't raise - we want to ensure the lock is released
            log_with_task_details('ERROR', f"Error releasing pipeline lock: {str(e)}", 
                task_id=None,
                details={'error': str(e)})

def update_task_status(task_id, status, conn=None):
    """Update task status with retry logic"""
    should_close_conn = False
    if conn is None:
        conn = get_db_connection()
        should_close_conn = True
    
    try:
        for attempt in range(3):  # Retry up to 3 times
            try:
                with conn:
                    c = conn.cursor()
                    c.execute("UPDATE tasks SET status=? WHERE id=?", (status, task_id))
                return True
            except sqlite3.OperationalError as e:
                if attempt == 2:  # Last attempt
                    raise
                time.sleep(1)  # Wait before retry
    finally:
        if should_close_conn:
            conn.close()

def get_preview_dir():
    """Get the absolute path to the previews directory"""
    preview_details = {
        'operation': 'get_preview_dir',
        'timestamp': datetime.now().isoformat()
    }
    
    try:
        webapp_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        preview_dir = os.path.join(webapp_root, 'static', 'previews')
        
        preview_details.update({
            'webapp_root': webapp_root,
            'preview_dir': preview_dir
        })
        
        os.makedirs(preview_dir, exist_ok=True)
        preview_details['directory_created'] = True
        
        log_with_details('INFO', f"Ensured preview directory exists",
            details=preview_details)
        return preview_dir
        
    except Exception as e:
        preview_details['error'] = str(e)
        log_with_details('ERROR', f"Failed to create preview directory",
            details=preview_details)
        raise

def process_video_pipeline(task_id, preview_mode=False, dry_run=False):
    """Main pipeline process for generating, processing, and uploading videos"""
    pipeline_details = {
        'task_id': task_id,
        'mode': 'dry_run' if dry_run else 'preview' if preview_mode else 'normal',
        'start_time': datetime.now().isoformat(),
        'preview_mode': preview_mode,
        'dry_run': dry_run
    }
    
    log_with_task_details('INFO', f"Starting video pipeline",
        task_id=task_id,
        details=pipeline_details)
    
    current_video_file = None
    safe_video_path = None
    conn = None
    
    if not (dry_run or preview_mode):
        # Update task status to running
        try:
            update_task_status(task_id, 'running')
        except Exception as e:
            log_with_task_details('ERROR', f"Failed to update task status", 
                task_id=task_id,
                details={'error': str(e), **pipeline_details})
    
    if not (dry_run or preview_mode) and not check_and_set_lock():
        log_with_task_details('INFO', "Another task is currently running. Task will retry later.",
            task_id=task_id,
            details=pipeline_details)
        return False if dry_run else None
    
    try:
        conn = get_db_connection()
        with conn:
            c = conn.cursor()
            
            if not (dry_run or preview_mode):
                c.execute("UPDATE task_lock SET task_id = ? WHERE id = 1", (task_id,))
                conn.commit()
                pipeline_details['lock_updated'] = True
            
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
                log_with_task_details('ERROR', "No task found with this ID",
                    task_id=task_id,
                    details=pipeline_details)
                return False if dry_run else None

            pipeline_details['task'] = {
                'id': task_data[0],
                'name': task_data[1],
                'generator_id': task_data[2],
                'has_utilities': bool(task_data[3]),
                'status': task_data[8],
                'email_notify': task_data[9]
            }

            task_dict = {
                'id': task_data[0],
                'name': task_data[1],
                'hashtags': task_data[5],
                'sound_name': task_data[6],
                'sound_volume': task_data[7]
            }

            try:
                # Generate initial video
                generator_details = {
                    **pipeline_details,
                    'generator_curl': task_data[-1]
                }
                
                if dry_run:
                    log_with_task_details('INFO', f"[DRY RUN] Would execute generator",
                        task_id=task_id,
                        details=generator_details)
                else:
                    log_with_task_details('INFO', "Executing generator",
                        task_id=task_id,
                        details=generator_details)
                        
                    success, stdout, stderr = execute_curl(task_data[-1], validate_output=True, mode='generator')
                    if not success:
                        error_msg = f"Generator failed: {stderr}"
                        log_with_task_details('ERROR', error_msg,
                            task_id=task_id,
                            details={'stdout': stdout, 'stderr': stderr, **generator_details})
                        raise Exception(error_msg)
                        
                    log_with_task_details('INFO', "Generator completed successfully",
                        task_id=task_id,
                        details=generator_details)

                    current_video_file = get_latest_video()
                    if not current_video_file:
                        error_msg = "No video file was generated"
                        log_with_task_details('ERROR', error_msg,
                            task_id=task_id,
                            details=generator_details)
                        raise Exception(error_msg)
                        
                    generator_details['current_video_file'] = current_video_file
                    log_with_task_details('INFO', f"Video file generated",
                        task_id=task_id,
                        details=generator_details)
                
                # Apply utilities in sequence
                if task_data[3]:  # utilities JSON string
                    utilities = json.loads(task_data[3])
                    utilities_details = {
                        **pipeline_details,
                        'utilities_count': len(utilities),
                        'utilities_ids': utilities
                    }
                    
                    log_with_task_details('INFO', f"Processing utilities",
                        task_id=task_id,
                        details=utilities_details)
                    
                    for index, util_id in enumerate(utilities, 1):
                        c.execute("SELECT id, name, utility_curl FROM utilities WHERE id=?", (util_id,))
                        util = c.fetchone()
                        
                        utility_details = {
                            **utilities_details,
                            'current_utility': {
                                'id': util_id,
                                'index': index,
                                'name': util[1] if util else None,
                                'found': bool(util)
                            }
                        }
                        
                        if not util:
                            log_with_task_details('WARNING', f"Utility not found",
                                task_id=task_id,
                                details=utility_details)
                            continue
                        
                        if dry_run:
                            log_with_task_details('INFO', f"[DRY RUN] Would run utility",
                                task_id=task_id,
                                details=utility_details)
                            continue
                            
                        log_with_task_details('INFO', f"Running utility",
                            task_id=task_id,
                            details=utility_details)
                        
                        util_cmd = util[2].format(input=current_video_file)
                        utility_details['command'] = util_cmd
                        
                        success, stdout, stderr = execute_curl(util_cmd, validate_output=True, mode='utility')
                        if not success:
                            error_msg = f"Utility failed: {stderr}"
                            utility_details.update({
                                'stdout': stdout,
                                'stderr': stderr
                            })
                            log_with_task_details('ERROR', error_msg,
                                task_id=task_id,
                                details=utility_details)
                            raise Exception(error_msg)
                            
                        log_with_task_details('INFO', f"Completed utility",
                            task_id=task_id,
                            details=utility_details)

                # Handle preview mode
                if preview_mode and current_video_file:
                    preview_dir = get_preview_dir()
                    preview_path = os.path.join(preview_dir, f'preview_task_{task_id}.mp4')
                    
                    preview_details = {
                        **pipeline_details,
                        'preview_path': preview_path,
                        'source_video': current_video_file
                    }
                    
                    if os.path.exists(preview_path):
                        log_with_task_details('INFO', "Removing existing preview file",
                            task_id=task_id,
                            details=preview_details)
                        os.remove(preview_path)
                        
                    log_with_task_details('INFO', "Saving preview file",
                        task_id=task_id,
                        details=preview_details)
                        
                    shutil.copy2(current_video_file, preview_path)
                    preview_details['preview_saved'] = True
                    
                    log_with_task_details('INFO', "Preview saved successfully",
                        task_id=task_id,
                        details=preview_details)
                        
                    update_task_status(task_id, 'completed', conn)
                    preview_details['status_updated'] = True
                    
                    log_with_task_details('INFO', "Preview task completed",
                        task_id=task_id,
                        details=preview_details)
                    return preview_path

                # Handle dry run mode
                if dry_run:
                    c.execute("""
                        SELECT p.name, tpa.account_name
                        FROM task_platform_accounts tpa
                        JOIN platforms p ON tpa.platform_id = p.id
                        WHERE tpa.task_id = ?
                    """, (task_id,))
                    platform_data = c.fetchall()
                    
                    dry_run_details = {
                        **pipeline_details,
                        'platforms': [
                            {'name': p_name, 'account': a_name}
                            for p_name, a_name in platform_data
                        ]
                    }
                    
                    if platform_data:
                        log_with_task_details('INFO', f"[DRY RUN] Would upload to platforms",
                            task_id=task_id,
                            details=dry_run_details)
                    return True

                # Skip uploads for preview mode
                if preview_mode:
                    return None

                # Process uploads
                upload_base_details = {
                    **pipeline_details,
                    'video_file': current_video_file
                }
                
                log_with_task_details('INFO', f"Preparing for uploads",
                    task_id=task_id,
                    details=upload_base_details)

                # Process uploads using the new schema
                c.execute("""
                    SELECT p.id, p.name, p.uploader_curl, tpa.account_name, p.default_hashtags, p.fallback_curl, p.fallback_curl_2
                    FROM task_platform_accounts tpa
                    JOIN platforms p ON tpa.platform_id = p.id
                    WHERE tpa.task_id = ?
                """, (task_id,))
                platform_data_list = c.fetchall()

                if platform_data_list:
                    upload_base_details['platform_count'] = len(platform_data_list)
                    log_with_task_details('INFO', f"Processing platform uploads",
                        task_id=task_id,
                        details=upload_base_details)
                    
                    uploaded_platforms = []  # Track successful uploads
                    
                    for platform_data in platform_data_list:
                        platform_id, platform_name, upload_curl, account_name, default_hashtags, fallback_curl, fallback_curl_2 = platform_data
                        
                        platform_details = {
                            **upload_base_details,
                            'platform': {
                                'id': platform_id,
                                'name': platform_name,
                                'account': account_name,
                                'has_default_hashtags': bool(default_hashtags)
                            }
                        }

                        log_with_task_details('INFO', f"Processing upload for platform",
                            task_id=task_id,
                            details=platform_details)

                        platform_dict = {
                            'account_name': account_name,
                            'default_hashtags': default_hashtags
                        }

                        # Format the upload command with all required parameters
                        upload_cmd, safe_video_path = format_upload_command(
                            upload_curl,
                            current_video_file,
                            task_dict,
                            platform_dict
                        )

                        if not upload_cmd or not safe_video_path:
                            error_msg = "Failed to prepare upload command or safe video path"
                            log_with_task_details('ERROR', error_msg,
                                task_id=task_id,
                                details=platform_details)
                            continue  # Skip this platform but continue with others

                        platform_details['safe_video_path'] = safe_video_path
                        success = False
                        current_stdout = ""
                        current_stderr = ""

                        # Try primary upload
                        success, stdout, stderr = execute_curl(upload_cmd, mode='uploader')
                        current_stdout, current_stderr = stdout, stderr

                        # If primary fails, try fallback
                        if not success and fallback_curl:
                            log_with_task_details('INFO', f"Primary upload failed, attempting fallback for {platform_name}",
                                task_id=task_id,
                                details={'primary_error': stderr, **platform_details})
                            fallback_cmd, _ = format_upload_command(
                                fallback_curl,
                                current_video_file,
                                task_dict,
                                platform_dict
                            )
                            success, stdout, stderr = execute_curl(fallback_cmd, mode='uploader')
                            if success:
                                current_stdout, current_stderr = stdout, stderr

                        # If fallback fails, try secondary fallback
                        if not success and fallback_curl_2:
                            log_with_task_details('INFO', f"Fallback upload failed, attempting secondary fallback for {platform_name}",
                                task_id=task_id,
                                details={'fallback_error': stderr, **platform_details})
                            fallback_cmd_2, _ = format_upload_command(
                                fallback_curl_2,
                                current_video_file,
                                task_dict,
                                platform_dict
                            )
                            success, stdout, stderr = execute_curl(fallback_cmd_2, mode='uploader')
                            if success:
                                current_stdout, current_stderr = stdout, stderr

                        if success:
                            uploaded_platforms.append(platform_name)
                            log_with_task_details('INFO', f"Successfully uploaded to {platform_name}",
                                task_id=task_id,
                                details={
                                    'stdout': current_stdout,
                                    'stderr': current_stderr,
                                    **platform_details
                                })
                        else:
                            error_msg = f"All upload attempts failed for {platform_name}: {current_stderr}"
                            log_with_task_details('ERROR', error_msg,
                                task_id=task_id,
                                details={
                                    'stdout': current_stdout,
                                    'stderr': current_stderr,
                                    **platform_details
                                })
                            # Don't raise exception, continue with other platforms

                    # After all platforms are processed
                    if not uploaded_platforms:
                        raise Exception("Failed to upload to any platform")

                    # Update task status and send notification for successful uploads
                    completion_details = {
                        **pipeline_details,
                        'status': 'completed',
                        'uploaded_platforms': uploaded_platforms
                    }

                    update_task_status(task_id, 'completed', conn)
                    completion_details['status_updated'] = True

                    if task_data[9]:  # If email notifications are enabled
                        try:
                            c.execute("SELECT id, name, email_notify FROM tasks WHERE id=?", (task_id,))
                            task_info = c.fetchone()
                            if task_info and task_info[2]:  # email_notify contains the email address
                                send_task_completion_notification(
                                    task_id, 
                                    task_info[1], 
                                    task_info[2], 
                                    success=True,
                                    platforms=uploaded_platforms
                                )
                                completion_details['notification_sent'] = True
                        except Exception as e:
                            log_with_task_details('ERROR', f"Failed to send completion notification",
                                task_id=task_id,
                                details={'error': str(e), **completion_details})

                    # Clean up video files
                    if current_video_file:
                        cleanup_video(current_video_file)
                        if safe_video_path and os.path.exists(safe_video_path):
                            cleanup_video(safe_video_path)
                        completion_details['videos_cleaned'] = True

                    log_with_task_details('INFO', "Task completed successfully",
                        task_id=task_id,
                        details=completion_details)
                    return True
                
            except Exception as e:
                error_details = {
                    **pipeline_details,
                    'error': str(e),
                    'error_type': type(e).__name__
                }
                
                log_with_task_details('ERROR', f"Pipeline error: {str(e)}",
                    task_id=task_id,
                    details=error_details)
                    
                # Update task status to failed
                try:
                    update_task_status(task_id, 'failed', conn)
                except Exception as status_error:
                    log_with_task_details('ERROR', f"Failed to update task status after error",
                        task_id=task_id,
                        details={'error': str(status_error), **error_details})
                
                raise
                
            finally:
                # Clean up any remaining files
                if current_video_file and not preview_mode:
                    cleanup_video(current_video_file)
                if safe_video_path and os.path.exists(safe_video_path):
                    try:
                        os.rename(safe_video_path, current_video_file)
                    except OSError:
                        pass
                
                # Always release the lock for real runs
                if not (dry_run or preview_mode):
                    release_lock()
                    
    except Exception as e:
        error_details = {
            **pipeline_details,
            'error': str(e),
            'error_type': type(e).__name__
        }
        
        log_with_task_details('ERROR', f"Task failed: {str(e)}",
            task_id=task_id,
            details=error_details)
        
        # Send failure notification if email notifications are enabled
        if not (dry_run or preview_mode):
            try:
                task_info = c.execute("SELECT id, name, email_notify FROM tasks WHERE id=?", (task_id,)).fetchone()
                if task_info and task_info[2]:
                    send_task_completion_notification(task_id, task_info[1], task_info[2], success=False)
                    error_details['failure_notification_sent'] = True
            except Exception as notify_error:
                error_details['notification_error'] = str(notify_error)
                log_with_task_details('ERROR', f"Failed to send error notification",
                    task_id=task_id,
                    details=error_details)
        
        raise
    finally:
        if conn:
            conn.close()