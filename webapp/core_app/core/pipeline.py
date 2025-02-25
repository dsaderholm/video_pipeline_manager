import json
import time
import os
import sys
import logging
import shutil
import threading
from datetime import datetime, timedelta
import logging
import sqlite3
logger = logging.getLogger('app')
from webapp.core_app.core.utils import execute_curl, get_latest_video, cleanup_video, format_upload_command, log_with_details
from webapp.core_app.core.email_utils import send_task_completion_notification
from webapp.core_app.core.database import db
from flask import current_app

# Global lock for thread safety (keep this as it's different from database locking)
db_lock = threading.Lock()

def log_with_task_details(level, message, task_id, details=None):
    """Helper function to log with task ID and structured details"""
    if details is None:
        details = {}
    details['task_id'] = task_id
    
    try:
        log_with_details(level, message, task_id=task_id, details=details, source='pipeline')
    except Exception as e:
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

def should_process_at_night():
    """Check if current time is within night processing window"""
    current_time = datetime.now().time()
    start_time = datetime.strptime(os.getenv('NIGHT_PROCESSING_START', '22:00'), '%H:%M').time()
    end_time = datetime.strptime(os.getenv('NIGHT_PROCESSING_END', '06:00'), '%H:%M').time()
    
    if start_time < end_time:
        return start_time <= current_time <= end_time
    else:  # Handles case where night window crosses midnight
        return current_time >= start_time or current_time <= end_time

def safe_rollback(conn, cursor=None):
    """Safely roll back a transaction with proper error handling"""
    if not conn:
        return False
        
    try:
        # Check if a transaction is active by trying a simple query
        if cursor is None:
            cursor = conn.cursor()
            
        cursor.execute("SELECT 1")
        
        # If we got here, connection is active, try to roll back
        try:
            conn.rollback()
            return True
        except sqlite3.OperationalError as e:
            # Log the rollback error but don't raise
            logger.warning(f"Rollback error: {str(e)}")
            return False
    except Exception as e:
        # Connection might be closed or invalid
        logger.warning(f"Cannot check transaction status: {str(e)}")
        return False

def check_and_set_lock(task_id=None):
    """Check if any task is running and set lock if not"""
    lock_details = {
        'operation': 'check_and_set',
        'timestamp': datetime.now().isoformat(),
        'task_id': task_id
    }
    
    with db_lock:
        conn = None
        try:
            # Create a new connection directly instead of using the contextmanager to avoid generator issues
            conn = db._create_connection()
            
            # First check current lock status
            c = conn.cursor()
            c.execute("SELECT locked, task_id, locked_at FROM task_lock WHERE id = 1")
            current_lock = c.fetchone()
            lock_details['current_lock'] = {
                'locked': current_lock[0] if current_lock else None,
                'task_id': current_lock[1] if current_lock else None,
                'locked_at': current_lock[2] if current_lock else None
            }
            
            # Clear stale locks (older than 5 minutes or NULL timestamp)
            c.execute("""
                UPDATE task_lock 
                SET locked = 0, task_id = NULL, locked_at = NULL 
                WHERE locked = 1 
                AND (
                    locked_at IS NULL 
                    OR datetime(locked_at, '+5 minutes') < datetime('now')
                )
            """)
            expired_cleared = c.rowcount > 0
            lock_details['expired_cleared'] = expired_cleared
            
            if expired_cleared:
                log_with_task_details('INFO', 
                    "Cleared expired lock",
                    task_id=task_id,
                    details={'cleared_lock': lock_details['current_lock']})
            
            # Try to acquire lock
            c.execute("""
                UPDATE task_lock 
                SET locked = 1, 
                    task_id = ?, 
                    locked_at = datetime('now')
                WHERE id = 1 AND locked = 0
            """, (task_id,))
            
            acquired = c.rowcount > 0
            lock_details['acquired'] = acquired
            
            # Get final lock status
            c.execute("SELECT locked, task_id, locked_at FROM task_lock WHERE id = 1")
            final_lock = c.fetchone()
            lock_details['final_lock'] = {
                'locked': final_lock[0] if final_lock else None,
                'task_id': final_lock[1] if final_lock else None,
                'locked_at': final_lock[2] if final_lock else None
            }
            
            conn.commit()
            
            log_with_task_details('INFO', 
                "Successfully acquired pipeline lock" if acquired else "Failed to acquire pipeline lock",
                task_id=task_id,
                details=lock_details)
            
            return acquired
                
        except Exception as e:
            lock_details['error'] = str(e)
            log_with_task_details('ERROR', 
                f"Error managing pipeline lock: {str(e)}", 
                task_id=task_id,
                details=lock_details)
            
            # Try to rollback if possible
            if conn:
                try:
                    safe_rollback(conn)
                except:
                    pass
            return False
        finally:
            # Always close the connection in finally block
            if conn:
                try:
                    conn.close()
                except:
                    pass

def release_lock(task_id=None):
    """Release the task lock"""
    release_details = {
        'operation': 'release',
        'timestamp': datetime.now().isoformat(),
        'task_id': task_id
    }
    
    with db_lock:
        conn = None
        try:
            # Create a new connection directly instead of using the contextmanager
            conn = db._create_connection()
            c = conn.cursor()
            
            # Get current lock status before release
            c.execute("SELECT locked, task_id, locked_at FROM task_lock WHERE id = 1")
            current_lock = c.fetchone()
            release_details['before_release'] = {
                'locked': current_lock[0] if current_lock else None,
                'task_id': current_lock[1] if current_lock else None,
                'locked_at': current_lock[2] if current_lock else None
            }
            
            # Only release if we own the lock
            if task_id:
                c.execute("""
                    UPDATE task_lock 
                    SET locked = 0, 
                        task_id = NULL, 
                        locked_at = NULL 
                    WHERE id = 1 
                    AND task_id = ?
                """, (task_id,))
            else:
                # Force release if no task_id provided
                c.execute("""
                    UPDATE task_lock 
                    SET locked = 0, 
                        task_id = NULL, 
                        locked_at = NULL 
                    WHERE id = 1
                """)
            released = c.rowcount > 0
            
            release_details['released'] = released
            
            # Get final lock status
            c.execute("SELECT locked, task_id, locked_at FROM task_lock WHERE id = 1")
            final_lock = c.fetchone()
            release_details['after_release'] = {
                'locked': final_lock[0] if final_lock else None,
                'task_id': final_lock[1] if final_lock else None,
                'locked_at': final_lock[2] if final_lock else None
            }
            
            conn.commit()
            
            if released:
                log_with_task_details('INFO', 
                    "Successfully released pipeline lock",
                    task_id=task_id,
                    details=release_details)
            else:
                log_with_task_details('DEBUG', 
                    "No lock needed to be released",
                    task_id=task_id,
                    details=release_details)
            
            return released
                
        except Exception as e:
            release_details['error'] = str(e)
            log_with_task_details('ERROR', 
                f"Error releasing pipeline lock: {str(e)}", 
                task_id=task_id,
                details=release_details)
            
            # Try to rollback if possible
            if conn:
                try:
                    safe_rollback(conn)
                except:
                    pass
                    
            # Try force release as last resort
            try:
                force_release_lock()
            except Exception as force_error:
                release_details['force_release_error'] = str(force_error)
                log_with_task_details('ERROR', 
                    f"Force release also failed: {str(force_error)}", 
                    task_id=task_id,
                    details=release_details)
            return False
        finally:
            # Always close the connection in finally block
            if conn:
                try:
                    conn.close()
                except:
                    pass

def update_task_status(task_id, status, processing_status=None, video_path=None, conn=None):
    """Update task status and processing details"""
    should_close_conn = conn is None
    try:
        if conn is None:
            with db.get_connection() as conn:
                c = conn.cursor()
                query = "UPDATE tasks SET status=?"
                params = [status]
                
                if processing_status is not None:
                    query += ", processing_status=?"
                    params.append(processing_status)
                
                if video_path is not None:
                    query += ", processed_video_path=?"
                    params.append(video_path)
                
                query += " WHERE id=?"
                params.append(task_id)
                
                c.execute(query, params)
    finally:
        if should_close_conn and conn:
            conn.close()

def store_generated_video(task_id, original_name, processed_path, scheduled_time, conn=None, in_transaction=False):
    """Store information about a newly generated video"""
    video_id = None
    should_close_conn = conn is None
    
    try:
        if conn is None:
            conn = db.get_connection()
            
        c = conn.cursor()
        
        # Only start transaction if not already in one
        if not in_transaction:
            c.execute("BEGIN IMMEDIATE")
        
        try:
            c.execute('''
                INSERT INTO generated_videos 
                (task_id, original_name, processed_path, scheduled_time, status, upload_status)
                VALUES (?, ?, ?, ?, 'completed', 'pending')
            ''', (task_id, original_name, processed_path, scheduled_time))
            
            video_id = c.lastrowid
            
            # Verify the video was stored
            c.execute("SELECT id FROM generated_videos WHERE id = ?", (video_id,))
            if not c.fetchone():
                raise Exception("Failed to verify video storage")
                
            # Only commit if we started the transaction
            if not in_transaction:
                c.execute("COMMIT")
            
            return video_id
            
        except Exception as e:
            # Only rollback if we started the transaction
            if not in_transaction:
                safe_rollback(conn, c)
            raise
            
    except Exception as e:
        log_with_task_details('ERROR', f"Failed to store video information: {str(e)}",
            task_id=task_id,
            details={
                'original_name': original_name,
                'processed_path': processed_path,
                'error': str(e)
            })
        raise
        
    finally:
        if should_close_conn and conn:
            conn.close()

def get_next_day_schedules(schedule_str):
    """Get tomorrow's schedule times from a schedule string"""
    tomorrow = datetime.now() + timedelta(days=1)
    tomorrow_day = tomorrow.strftime('%A')[:3].lower()
    
    scheduled_times = []
    day_schedules = schedule_str.split(';')
    
    for day_schedule in day_schedules:
        if not day_schedule.strip():
            continue
        
        day, times = day_schedule.split('|')
        if day.lower() == tomorrow_day:
            for time_str in times.split(','):
                hour, minute = map(int, time_str.strip().split(':'))
                schedule_time = tomorrow.replace(hour=hour, minute=minute, second=0, microsecond=0)
                scheduled_times.append(schedule_time)
    
    return scheduled_times

def process_video_generation(task_id, schedule_time=None, preview_mode=False, dry_run=False, conn=None, parent_has_lock=False):
    """Handle video generation and utility processing"""
    lock_acquired = False
    generation_details = {
        'task_id': task_id,
        'mode': 'dry_run' if dry_run else 'preview' if preview_mode else 'normal',
        'schedule_time': schedule_time.isoformat() if schedule_time else None,
        'start_time': datetime.now().isoformat()
    }
    
    log_with_task_details('INFO', f"Starting video generation",
        task_id=task_id,
        details=generation_details)
        
    files_to_cleanup = set()
    current_video_file = None
    video_id = None
    should_close_conn = conn is None
    
    try:
        # Only acquire lock if parent doesn't have one
        if not (dry_run or preview_mode or parent_has_lock):
            lock_acquired = check_and_set_lock(task_id)
            if not lock_acquired:
                log_with_task_details('INFO', "Another task is currently running",
                    task_id=task_id,
                    details=generation_details)
                return None

        if conn is None:
            conn = db.get_connection()
            
        c = conn.cursor()
        # Start a transaction for the whole generation process
        c.execute("BEGIN IMMEDIATE")
        
        try:
            c.execute("""
                SELECT t.id, t.name, t.generator_id, t.utilities, t.schedule, 
                       t.hashtags, t.sound_name, t.sound_volume, t.status, 
                       t.email_notify, t.created_at,
                       g.generator_curl 
                FROM tasks t
                JOIN generators g ON t.generator_id = g.id
                WHERE t.id=?
            """, (task_id,))
            task_data = c.fetchone()
            
            if not task_data:
                log_with_task_details('ERROR', "No task found with this ID",
                    task_id=task_id,
                    details=generation_details)
                return None

            # Generate video
            if dry_run:
                log_with_task_details('INFO', "[DRY RUN] Would execute generator",
                    task_id=task_id,
                    details=generation_details)
                return True

            try:
                success, stdout, original_filename = execute_curl(task_data[-1], retries=3, retry_delay=5, validate_output=True, mode='generator')
                if not success:
                    error_msg = f"Generator failed: {original_filename}"  # original_filename contains error in case of failure
                    log_with_task_details('ERROR', error_msg,
                        task_id=task_id,
                        details={'stdout': stdout, 'stderr': original_filename, **generation_details})
                    update_task_status(task_id, 'failed', 'failed', None, conn)
                    return None
            except Exception as e:
                # Make sure to clean up any temporary files
                if current_video_file and os.path.exists(current_video_file):
                    cleanup_video(current_video_file)
                # Check if transaction is active before rolling back
                try:
                    c.execute("SELECT 1")  # Quick test if transaction is active
                    safe_rollback(conn, c)
                except sqlite3.OperationalError:
                    # Transaction wasn't active, that's okay
                    pass
                return None

            current_video_file = get_latest_video()
            if not current_video_file:
                error_msg = "No video file was generated"
                log_with_task_details('ERROR', error_msg,
                    task_id=task_id,
                    details=generation_details)
                update_task_status(task_id, 'failed', 'failed', None, conn)
                raise Exception(error_msg)

            original_name = os.path.splitext(os.path.basename(current_video_file))[0]
            log_with_task_details('INFO', f"Using generated video filename",
                task_id=task_id,
                details={'video_name': original_name})

            files_to_cleanup.add(current_video_file)

            # Apply utilities
            if task_data[3]:  # utilities JSON string
                utilities = json.loads(task_data[3])
                for util_id in utilities:
                    c.execute("SELECT utility_curl FROM utilities WHERE id=?", (util_id,))
                    util = c.fetchone()
                    if not util:
                        continue

                    try:
                        util_cmd = util[0].format(input=current_video_file)
                        success, stdout, stderr = execute_curl(util_cmd, retries=3, retry_delay=5, validate_output=True, mode='utility')
                        if not success:
                            error_msg = f"Utility failed: {stderr}"
                            log_with_task_details('ERROR', error_msg,
                                task_id=task_id,
                                details={'stdout': stdout, 'stderr': stderr, **generation_details})
                            update_task_status(task_id, 'failed', 'failed', None, conn)
                            raise Exception(error_msg)
                    except Exception as e:
                        # Clean up on utility failure
                        if current_video_file and os.path.exists(current_video_file):
                            cleanup_video(current_video_file)
                        safe_rollback(conn, c)
                        raise

            # Handle preview mode
            if preview_mode:
                preview_dir = os.path.join('static', 'previews')
                os.makedirs(preview_dir, exist_ok=True)
                preview_path = os.path.join(preview_dir, f'preview_task_{task_id}.mp4')
                
                if os.path.exists(preview_path):
                    os.remove(preview_path)
                
                shutil.copy2(current_video_file, preview_path)
                update_task_status(task_id, 'completed', None, None, conn)
                c.execute("COMMIT")
                return preview_path

            # Store processed video
            os.makedirs('processed_videos', exist_ok=True)
            timestamp = int(time.time())
            permanent_path = os.path.join('processed_videos', f'task_{task_id}_{timestamp}.mp4')
            shutil.copy2(current_video_file, permanent_path)
            
            # Store video information with schedule time
            if schedule_time:
                video_id = store_generated_video(
                    task_id, 
                    original_name,
                    permanent_path, 
                    schedule_time.isoformat(), 
                    conn,
                    in_transaction=True
                )
            else:
                video_id = store_generated_video(
                    task_id, 
                    original_name,
                    permanent_path, 
                    datetime.now().isoformat(), 
                    conn,
                    in_transaction=True
                )
                
            if not video_id:
                raise Exception("Failed to store video information")
            
            update_task_status(task_id, 'pending', 'processed', permanent_path, conn)
            
            # Commit the transaction if everything succeeded
            c.execute("COMMIT")
            
            log_with_task_details('INFO', f"Video generation completed successfully",
                task_id=task_id,
                details={
                    'video_path': permanent_path,
                    'original_name': original_name,
                    'video_id': video_id,
                    **generation_details
                })
            return permanent_path, video_id
            
        except Exception as e:
            # Rollback transaction on error
            safe_rollback(conn, c)
            raise
            
    except Exception as e:
        log_with_task_details('ERROR', f"Video generation failed: {str(e)}",
            task_id=task_id,
            details={'error': str(e), **generation_details})
        if not (dry_run or preview_mode):
            update_task_status(task_id, 'failed', 'failed', None, conn)
        raise

    finally:
        # Always attempt to release lock in finally block
        if lock_acquired:
            try:
                release_lock(task_id)
            except Exception as e:
                log_with_task_details('ERROR', 
                    f"Failed to release lock, attempting force release: {str(e)}",
                    task_id=task_id,
                    details={'error': str(e)})
                try:
                    force_release_lock()
                except Exception as force_e:
                    log_with_task_details('ERROR', 
                        f"Force release also failed: {str(force_e)}",
                        task_id=task_id,
                        details={'error': str(force_e)})
        
        cleanup_files(files_to_cleanup)
        
        # Only close the connection if we created it
        if should_close_conn and conn:
            try:
                conn.close()
            except Exception as e:
                log_with_task_details('ERROR', 
                    f"Failed to close database connection: {str(e)}",
                    task_id=task_id,
                    details={'error': str(e)})

def process_video_upload(task_id, video_info=None, preview_mode=False, dry_run=False, conn=None):
    """Handle video uploading to platforms"""
    upload_details = {
        'task_id': task_id,
        'mode': 'dry_run' if dry_run else 'preview' if preview_mode else 'normal',
        'start_time': datetime.now().isoformat()
    }
    
    if video_info:
        upload_details['video_id'] = video_info[0]
        upload_details['scheduled_time'] = video_info[3]
    
    log_with_task_details('INFO', f"Starting video upload process",
        task_id=task_id,
        details=upload_details)
    
    files_to_cleanup = set()
    
    try:
        c = conn.cursor()
        
        # Get video to upload
        if video_info:
            video_id, original_name, processed_path, scheduled_time = video_info
            log_with_task_details('INFO', f"Using provided video info",
                task_id=task_id,
                details={'video_id': video_id, 'original_name': original_name})
        else:
            # Get the next pending video for this task
            c.execute("""
                SELECT id, original_name, processed_path, scheduled_time
                FROM generated_videos 
                WHERE task_id = ? 
                AND upload_status = 'pending'
                AND scheduled_time <= datetime('now', 'localtime')
                ORDER BY scheduled_time ASC
                LIMIT 1
            """, (task_id,))
            video_info = c.fetchone()
            if not video_info:
                log_with_task_details('INFO', "No pending videos ready for upload",
                    task_id=task_id,
                    details=upload_details)
                return False
            
            video_id, original_name, processed_path, scheduled_time = video_info
            log_with_task_details('INFO', f"Found pending video",
                task_id=task_id,
                details={'video_id': video_id, 'original_name': original_name})

        if not video_id:
            log_with_task_details('ERROR', "Video ID is null",
                task_id=task_id,
                details=upload_details)
            return False

        if not processed_path or not os.path.exists(processed_path):
            log_with_task_details('ERROR', f"Video file not found: {processed_path}",
                task_id=task_id,
                details={'video_id': video_id, 'processed_path': processed_path})
            c.execute("""
                UPDATE generated_videos 
                SET status = 'failed',
                    upload_status = 'failed',
                    error_message = ?
                WHERE id = ?
            """, ("Video file not found", video_id))
            return False

        # Get task data
        c.execute("""
            SELECT t.name, t.hashtags, t.sound_name, t.sound_volume, t.email_notify
            FROM tasks t
            WHERE t.id = ?
        """, (task_id,))
        task_data = c.fetchone()
        
        if not task_data:
            log_with_task_details('ERROR', "Task not found",
                task_id=task_id,
                details={'video_id': video_id})
            return False

        # Use the original name directly from video_info since we already have it
        original_filename = os.path.splitext(original_name)[0]  # Remove extension
        log_with_task_details('INFO', f"Using original filename for description",
            task_id=task_id,
            details={'original_filename': original_filename})

        task_dict = {
            'id': task_id,
            'name': task_data[0],
            'hashtags': task_data[1],
            'sound_name': task_data[2],
            'sound_volume': task_data[3],
            'original_name': original_name  # Pass the full original name from database
        }

        if dry_run:
            return True

        # Process uploads
        c.execute("""
            SELECT p.id, p.name, p.uploader_curl, tpa.account_name, 
                   p.default_hashtags, p.fallback_curl, p.fallback_curl_2
            FROM task_platform_accounts tpa
            JOIN platforms p ON tpa.platform_id = p.id
            WHERE tpa.task_id = ?
        """, (task_id,))
        platform_data_list = c.fetchall()

        if not platform_data_list:
            log_with_task_details('ERROR', "No platforms configured for upload",
                task_id=task_id,
                details=upload_details)
            return False

        uploaded_platforms = []
        for platform_data in platform_data_list:
            platform_id, platform_name, upload_curl, account_name, default_hashtags, fallback_curl, fallback_curl_2 = platform_data
            
            platform_details = {
                **upload_details,
                'platform': {
                    'id': platform_id,
                    'name': platform_name,
                    'account': account_name,
                    'has_default_hashtags': bool(default_hashtags),
                    'has_fallback': bool(fallback_curl),
                    'has_fallback_2': bool(fallback_curl_2)
                }
            }
            
            # Create a copy of the video with original name + platform suffix
            original_name_base = os.path.splitext(original_name)[0]
            platform_video_file = f"{os.path.join(os.path.dirname(processed_path), original_name_base)}_{platform_name}.mp4"
            shutil.copy2(processed_path, platform_video_file)
            files_to_cleanup.add(platform_video_file)

            platform_dict = {
                'account_name': account_name,
                'default_hashtags': default_hashtags
            }

            # Try primary upload
            upload_cmd, safe_video_path = format_upload_command(
                upload_curl,
                platform_video_file,
                task_dict,
                platform_dict
            )

            if safe_video_path:
                files_to_cleanup.add(safe_video_path)

            if not upload_cmd:
                log_with_task_details('ERROR', f"Failed to prepare upload command for {platform_name}",
                    task_id=task_id,
                    details=platform_details)
                continue

            success = False
            current_stdout = ""
            current_stderr = ""

            # Try primary upload
            success, stdout, stderr = execute_curl(upload_cmd, retries=3, retry_delay=5, mode='uploader')
            current_stdout, current_stderr = stdout, stderr

            # If primary fails, try fallback
            if not success and fallback_curl:
                log_with_task_details('INFO', f"Primary upload failed, attempting fallback for {platform_name}",
                    task_id=task_id,
                    details={'primary_error': stderr, **platform_details})
                
                fallback_cmd, fallback_safe_path = format_upload_command(
                    fallback_curl,
                    platform_video_file,
                    task_dict,
                    platform_dict
                )
                
                if fallback_safe_path:
                    files_to_cleanup.add(fallback_safe_path)
                    
                success, stdout, stderr = execute_curl(fallback_cmd, retries=3, retry_delay=5, mode='uploader')
                if success:
                    current_stdout, current_stderr = stdout, stderr

            # If fallback fails, try secondary fallback
            if not success and fallback_curl_2:
                log_with_task_details('INFO', f"Fallback upload failed, attempting secondary fallback for {platform_name}",
                    task_id=task_id,
                    details={'fallback_error': stderr, **platform_details})
                
                fallback_cmd_2, fallback_safe_path_2 = format_upload_command(
                    fallback_curl_2,
                    platform_video_file,
                    task_dict,
                    platform_dict
                )
                
                if fallback_safe_path_2:
                    files_to_cleanup.add(fallback_safe_path_2)
                    
                success, stdout, stderr = execute_curl(fallback_cmd_2, retries=3, retry_delay=5, mode='uploader')
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

        # After all platforms are processed
        if not uploaded_platforms:
            c.execute("""
                UPDATE generated_videos 
                SET status = 'failed',
                    upload_status = 'failed',
                    error_message = ?,
                    retry_count = retry_count + 1
                WHERE id = ?
            """, ("Failed to upload to any platform", video_id))
            update_task_status(task_id, 'failed', None, None, conn)
            raise Exception("Failed to upload to any platform")

        # Update video and task status
        c.execute("""
            UPDATE generated_videos 
            SET status = 'completed',
                upload_status = 'completed'
            WHERE id = ?
        """, (video_id,))

        # Check if this is the last pending video for the task
        c.execute("""
            SELECT COUNT(*) 
            FROM generated_videos 
            WHERE task_id = ? AND upload_status = 'pending'
        """, (task_id,))
        pending_count = c.fetchone()[0]

        if pending_count == 0:
            update_task_status(task_id, 'completed', 'completed', None, conn)

        # Send email notification if enabled
        if task_data[4]:  # email_notify
            try:
                send_task_completion_notification(
                    task_id, 
                    task_data[0],  # task name
                    task_data[4],  # email address
                    success=True,
                    platforms=uploaded_platforms
                )
            except Exception as e:
                log_with_task_details('ERROR', f"Failed to send completion notification",
                    task_id=task_id,
                    details={'error': str(e), **upload_details})

        return True

    except Exception as e:
        log_with_task_details('ERROR', f"Upload process failed: {str(e)}",
            task_id=task_id,
            details={'error': str(e), **upload_details})
        if not (dry_run or preview_mode):
            if video_info:
                c.execute("""
                    UPDATE generated_videos 
                    SET status = 'failed',
                        upload_status = 'failed',
                        error_message = ?,
                        retry_count = retry_count + 1
                    WHERE id = ?
                """, (str(e), video_id))
            update_task_status(task_id, 'failed', None, None, conn)
        raise

    finally:
        cleanup_files(files_to_cleanup)

def process_video_pipeline(task_id, schedule_time=None, preview_mode=False, dry_run=False):
    """Main pipeline process that coordinates generation and upload"""
    pipeline_details = {
        'task_id': task_id,
        'mode': 'dry_run' if dry_run else 'preview' if preview_mode else 'normal',
        'schedule_time': schedule_time.isoformat() if schedule_time else None,
        'start_time': datetime.now().isoformat()
    }
    
    log_with_task_details('INFO', f"Starting video pipeline",
        task_id=task_id,
        details=pipeline_details)
    
    # Don't update status or acquire lock for dry runs and previews
    if not (dry_run or preview_mode):
        update_task_status(task_id, 'running', 'pending')
    
    lock_acquired = False
    try:
        # Lock handling
        if not (dry_run or preview_mode):
            lock_acquired = check_and_set_lock(task_id)
            if not lock_acquired:
                log_with_task_details('INFO', 
                    "Another task is currently running. Task will retry later.",
                    task_id=task_id,
                    details=pipeline_details)
                return None
        
        with db.get_connection() as conn:
            # Night processing check
            if not (preview_mode or dry_run) and not should_process_at_night():
                # Only proceed if this is a manual run
                if not getattr(current_app, 'manual_run', False):
                    log_with_task_details('INFO', 
                        "Outside night processing window, task will be processed later.",
                        task_id=task_id,
                        details=pipeline_details)
                    return None
            
            # Generate video
            try:
                generation_result = process_video_generation(
                    task_id, 
                    schedule_time, 
                    preview_mode, 
                    dry_run, 
                    conn,
                    parent_has_lock=lock_acquired
                )
            except Exception as e:
                log_with_task_details('ERROR', 
                    f"Video generation failed: {str(e)}",
                    task_id=task_id,
                    details={'error': str(e), **pipeline_details})
                raise
            
            if preview_mode:
                return generation_result[0] if isinstance(generation_result, tuple) else generation_result
            
            if dry_run:
                return True
            
            # Handle upload for normal runs
            if generation_result and isinstance(generation_result, tuple):
                video_path, video_id = generation_result
                pipeline_details['video_info'] = {
                    'path': video_path,
                    'id': video_id
                }
                
                # Immediate upload for manual runs or catchup
                if not schedule_time or schedule_time <= datetime.now():
                    try:
                        # Get the original name from the video path
                        original_name = os.path.basename(video_path)

                        upload_result = process_video_upload(
                            task_id, 
                            (video_id, original_name, video_path, datetime.now().isoformat()),
                            preview_mode, 
                            dry_run, 
                            conn
                        )
                        pipeline_details['upload_result'] = upload_result
                        return upload_result
                    except Exception as e:
                        log_with_task_details('ERROR', 
                            f"Video upload failed: {str(e)}",
                            task_id=task_id,
                            details={'error': str(e), **pipeline_details})
                        raise
                
                return True  # Video generated successfully, will be uploaded at scheduled time
            
            return None
            
    except Exception as e:
        log_with_task_details('ERROR', 
            f"Pipeline failed: {str(e)}",
            task_id=task_id,
            details={'error': str(e), **pipeline_details})
        if not (dry_run or preview_mode):
            update_task_status(task_id, 'failed', 'failed')
        raise
        
    finally:
        # Always release lock if we acquired it
        if lock_acquired:
            try:
                release_lock(task_id)
            except Exception as e:
                log_with_task_details('ERROR', 
                    f"Failed to release lock, attempting force release: {str(e)}",
                    task_id=task_id,
                    details={'error': str(e), **pipeline_details})
                # If normal release fails, try force release
                try:
                    force_release_lock()
                except Exception as force_e:
                    log_with_task_details('ERROR', 
                        f"Force release also failed: {str(force_e)}",
                        task_id=task_id,
                        details={'error': str(force_e), **pipeline_details})

def process_night_queue():
    """Process pending tasks during night window"""
    if not should_process_at_night():
        logger.debug("Outside night processing window, skipping night queue")
        return

    lock_acquired = False
    try:
        # Try to acquire lock before processing
        if not check_and_set_lock():
            logger.debug("Could not acquire lock for night processing, will retry next cycle")
            return

        lock_acquired = True
        with db.get_connection() as conn:
            c = conn.cursor()
            
            # Get all tasks that need processing for tomorrow
            tomorrow = datetime.now() + timedelta(days=1)
            tomorrow_day = tomorrow.strftime('%A')[:3].lower()
            
            c.execute("""
                SELECT id, schedule 
                FROM tasks 
                WHERE status != 'failed'
                AND processing_status != 'failed'
                AND schedule LIKE ?
            """, (f'%{tomorrow_day}|%',))
            
            tasks = c.fetchall()

            for task_id, schedule in tasks:
                schedule_times = get_next_day_schedules(schedule)
                if not schedule_times:
                    continue
                    
                try:
                    # Generate a video for each scheduled time
                    for schedule_time in schedule_times:
                        process_video_generation(task_id, schedule_time, conn=conn)
                except Exception as e:
                    log_with_task_details('ERROR', 
                        f"Night processing failed for task {task_id}: {str(e)}",
                        task_id=task_id,
                        details={'error': str(e), 'schedule_time': schedule_time})

    except Exception as e:
        logger.error(f"Error in night processing queue: {str(e)}")
        
    finally:
        if lock_acquired:
            try:
                release_lock()
            except Exception as e:
                logger.error(f"Failed to release lock after night processing: {str(e)}")
                try:
                    force_release_lock()
                except Exception as force_e:
                    logger.error(f"Force release also failed after night processing: {str(force_e)}")

def process_scheduled_uploads():
    """Process tasks that are ready for upload"""
    lock_acquired = False
    try:
        if not check_and_set_lock():
            logger.debug("Task lock is held, skipping scheduled uploads")
            return
            
        lock_acquired = True
        with db.get_connection() as conn:
            c = conn.cursor()
            c.execute('''
                SELECT v.task_id, v.id, v.original_name, v.processed_path, v.scheduled_time
                FROM generated_videos v
                JOIN tasks t ON v.task_id = t.id
                WHERE v.upload_status = 'pending'
                AND v.scheduled_time <= datetime('now', 'localtime')
                AND t.status != 'failed'
                ORDER BY v.scheduled_time ASC
                LIMIT 1
            ''')
            pending_upload = c.fetchone()
            
            if pending_upload:
                task_id, video_id, original_name, processed_path, scheduled_time = pending_upload
                try:
                    process_video_upload(
                        task_id, 
                        (video_id, original_name, processed_path, scheduled_time), 
                        conn=conn
                    )
                except Exception as e:
                    log_with_task_details('ERROR', 
                        f"Failed to process upload for task {task_id}: {str(e)}",
                        task_id=task_id,
                        details={
                            'error': str(e),
                            'video_id': video_id,
                            'scheduled_time': scheduled_time
                        })
    except Exception as e:
        logger.error(f"Error in scheduled uploads processor: {str(e)}")
        
    finally:
        if lock_acquired:
            try:
                release_lock()
            except Exception as e:
                logger.error(f"Failed to release lock after scheduled uploads: {str(e)}")
                try:
                    force_release_lock()
                except Exception as force_e:
                    logger.error(f"Force release also failed after scheduled uploads: {str(force_e)}")

def cleanup_files(video_files):
    """Cleanup multiple video files with error handling"""
    for file in video_files:
        if file and os.path.exists(file):
            try:
                cleanup_video(file)
            except Exception as e:
                log_with_details('ERROR', f"Failed to cleanup file {file}: {str(e)}",
                    details={'file': file, 'error': str(e)})
                    
def force_release_lock():
    """Force release any existing lock regardless of state"""
    release_details = {
        'operation': 'force_release',
        'timestamp': datetime.now().isoformat()
    }
    
    conn = None
    try:
        # Create a direct connection instead of using contextmanager
        conn = db._create_connection()
        c = conn.cursor()
        
        # Get current lock state for logging
        c.execute("SELECT locked, task_id, locked_at FROM task_lock WHERE id = 1")
        current_lock = c.fetchone()
        release_details['before_release'] = {
            'locked': current_lock[0] if current_lock else None,
            'task_id': current_lock[1] if current_lock else None,
            'locked_at': current_lock[2] if current_lock else None
        }
        
        # Force release the lock
        c.execute("""
            UPDATE task_lock 
            SET locked = 0, task_id = NULL, locked_at = NULL 
            WHERE id = 1
        """)
        
        released = c.rowcount > 0
        release_details['released'] = released
        
        # Get final state for logging
        c.execute("SELECT locked, task_id, locked_at FROM task_lock WHERE id = 1")
        final_lock = c.fetchone()
        release_details['after_release'] = {
            'locked': final_lock[0] if final_lock else None,
            'task_id': final_lock[1] if final_lock else None,
            'locked_at': final_lock[2] if final_lock else None
        }
        
        conn.commit()
        
        log_with_task_details('INFO', 
            "Successfully force-released pipeline lock" if released else "No lock needed to be released",
            task_id=None,
            details=release_details)
        
        return released
        
    except Exception as e:
        release_details['error'] = str(e)
        log_with_task_details('ERROR', 
            f"Failed to force release lock: {str(e)}", 
            task_id=None,
            details=release_details)
        
        # Try to rollback if possible
        if conn:
            try:
                safe_rollback(conn)
            except:
                pass
        
        return False
    finally:
        # Always close the connection in finally block
        if conn:
            try:
                conn.close()
            except:
                pass