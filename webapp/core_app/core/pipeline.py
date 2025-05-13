import json
import time
import os
import sys
import logging
import shutil
import threading
import subprocess
from datetime import datetime, timedelta
import logging
import sqlite3
logger = logging.getLogger('app')
# Import log_manager for compatibility, but don't use its handlers
from webapp.core_app.core.log_manager import db_log_handler

# No need to add the handler - we're using Docker logs
# Keep this for compatibility with existing code
from webapp.core_app.core.utils import execute_curl, get_latest_video, cleanup_video, format_upload_command, log_with_details, cleanup_existing_mp4s, validate_video_file
# Remove the direct import to avoid circular dependencies - import at function level instead
# from webapp.core_app.core.email_utils import send_task_completion_notification
from webapp.core_app.core.database import db
from flask import current_app

# Global lock for thread safety (keep this as it's different from database locking)
db_lock = threading.Lock()

def check_connection_health(conn):
    """Verify if a database connection is healthy and active"""
    if conn is None:
        return False
        
    try:
        # Try a simple query to verify connection works
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        result = cursor.fetchone()
        return result is not None and result[0] == 1
    except Exception as e:
        logger.warning(f"Database connection health check failed: {str(e)}")
        return False

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
    
    # No need to add db_log_handler - using Docker logs
    # Keep this section for compatibility, but don't add the handler
        
    return logger_instance

def should_process_at_night():
    """Check if current time is within night processing window
    
    Handles cases where the night window crosses midnight by considering
    the full date+time instead of just the time component.
    """
    now = datetime.now()
    current_time = now.time()
    
    # Get start and end times from environment or use defaults
    start_time_str = os.getenv('NIGHT_PROCESSING_START', '22:00')
    end_time_str = os.getenv('NIGHT_PROCESSING_END', '06:00')
    
    # Parse the time strings
    start_time = datetime.strptime(start_time_str, '%H:%M').time()
    end_time = datetime.strptime(end_time_str, '%H:%M').time()
    
    # Create full datetime objects for comparison
    start_datetime = datetime.combine(now.date(), start_time)
    end_datetime = datetime.combine(now.date(), end_time)
    
    # If end time is earlier than start time, it crosses midnight, so add a day
    if end_time < start_time:
        end_datetime = end_datetime + timedelta(days=1)
    
    # Check if current time is in the window
    current_datetime = datetime.combine(now.date(), current_time)
    
    # Handle the case where we're after midnight but before end time
    if current_time < end_time and start_time > end_time:
        # We're in the early morning hours, so start time was yesterday
        yesterday_start = start_datetime - timedelta(days=1)
        return yesterday_start <= current_datetime <= end_datetime
    
    # Normal check if current time is between start and end
    return start_datetime <= current_datetime <= end_datetime

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
    """Check if any task is running and set lock if not
    
    If this function is called with the same task_id that currently holds
    the lock, it will return True (success) to prevent self-deadlock.
    """
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
            
            # Verify the connection is healthy
            if not check_connection_health(conn):
                log_with_task_details('ERROR', 
                    "Failed to create a healthy database connection for lock check", 
                    task_id=task_id,
                    details=lock_details)
                return False
                
            c = conn.cursor()
            
            # Start transaction to prevent race conditions
            db.begin_transaction(conn)
            
            # First check current lock status
            c.execute("SELECT locked, task_id, locked_at FROM task_lock WHERE id = 1")
            current_lock = c.fetchone()
            lock_details['current_lock'] = {
                'locked': current_lock[0] if current_lock else None,
                'task_id': current_lock[1] if current_lock else None,
                'locked_at': current_lock[2] if current_lock else None
            }
            
            # Check if this task already holds the lock
            if current_lock and current_lock[0] == 1 and current_lock[1] == task_id and task_id is not None:
                # This task already holds the lock, so return success
                db.commit_transaction(conn)
                log_with_task_details('INFO', 
                    "Task already holds the lock, returning success",
                    task_id=task_id,
                    details=lock_details)
                return True
            
            # Clear stale locks (older than 10 minutes or NULL timestamp)
            c.execute("""
                UPDATE task_lock 
                SET locked = 0, task_id = NULL, locked_at = NULL 
                WHERE locked = 1 
                AND (
                    locked_at IS NULL 
                    OR datetime(locked_at, '+10 minutes') < datetime('now')
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
            
            # Commit the transaction
            db.commit_transaction(conn)
            
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
                    db.rollback_transaction(conn)
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
            
            # Verify the connection is healthy
            if not check_connection_health(conn):
                log_with_task_details('ERROR', 
                    "Failed to create a healthy database connection for lock release", 
                    task_id=task_id,
                    details=release_details)
                # Try force release as fallback
                try:
                    return force_release_lock()
                except Exception as force_e:
                    release_details['force_error'] = str(force_e)
                    log_with_task_details('ERROR', 
                        f"Force release failed after connection health check: {str(force_e)}", 
                        task_id=task_id,
                        details=release_details)
                return False
                
            c = conn.cursor()
            
            # Start transaction to prevent race conditions
            db.begin_transaction(conn)
            
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
            
            # Commit the transaction
            db.commit_transaction(conn)
            
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
                    db.rollback_transaction(conn)
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

def process_video_generation(task_id, schedule_time=None, preview_mode=False, conn=None, parent_has_lock=False):
    """Handle video generation and utility processing"""
    lock_acquired = False
    generation_details = {
        'task_id': task_id,
        'mode': 'preview' if preview_mode else 'normal',
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
        if not (preview_mode or parent_has_lock):
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
                
            # Log task data to debug
            log_with_task_details('INFO', "Retrieved task data",
                task_id=task_id,
                details={
                    'task_id': task_data[0],
                    'name': task_data[1], 
                    'generator_id': task_data[2],
                    'utilities': task_data[3],
                    'schedule': task_data[4],
                    'generator_curl': task_data[-1]
                })

            # Generate video - First cleanup any existing files
            try:
                log_with_task_details('INFO', "Calling cleanup_existing_mp4s", task_id=task_id)
                cleanup_existing_mp4s()
                log_with_task_details('INFO', "Completed cleanup_existing_mp4s", task_id=task_id)
            except Exception as cleanup_error:
                log_with_task_details('ERROR', f"Error in cleanup_existing_mp4s: {str(cleanup_error)}", task_id=task_id)
                raise
            
            try:
                # Log the generator command that will be executed
                log_with_task_details('INFO', f"Executing generator command",
                    task_id=task_id,
                    details={'generator_curl': task_data[-1]})
                    
                # Execute the generator command with increased retries and delay
                success, stdout, original_filename = execute_curl(
                    task_data[-1], 
                    retries=3, 
                    retry_delay=5, 
                    clean_before=True,  # Ensure clean state
                    validate_output=True, 
                    timeout=600,  # 10 minute timeout
                    mode='generator'
                )
                
                if not success:
                    error_msg = f"Generator failed: {original_filename}"  # original_filename contains error in case of failure
                    log_with_task_details('ERROR', error_msg,
                        task_id=task_id,
                        details={'stdout': stdout, 'stderr': original_filename, **generation_details})
                    update_task_status(task_id, 'failed', 'failed', None, conn)
                    return None
                
                # Add delay to ensure file system operations are complete
                time.sleep(3)
                
                # Log success
                log_with_task_details('INFO', f"Generator command executed successfully",
                    task_id=task_id,
                    details={'original_filename': original_filename})
            except Exception as e:
                # Log the exception
                log_with_task_details('ERROR', f"Generator execution failed with exception: {str(e)}",
                    task_id=task_id,
                    details={'error': str(e), **generation_details})
                    
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
                    
                update_task_status(task_id, 'failed', 'failed', None, conn)
                return None

            # Find the generated video file with additional logging
            log_with_task_details('INFO', f"Searching for generated video file",
                task_id=task_id,
                details={'current_dir': os.getcwd()})
                
            # List files in current directory to help with debugging
            try:
                files = os.listdir('.')
                mp4_files = [f for f in files if f.lower().endswith('.mp4')]
                log_with_task_details('INFO', f"Files in directory after generator execution", 
                    task_id=task_id,
                    details={'all_files': files, 'mp4_files': mp4_files})
            except Exception as list_e:
                log_with_task_details('WARNING', f"Error listing directory contents: {str(list_e)}", 
                    task_id=task_id,
                    details={'error': str(list_e)})
            
            # Try to get the latest video multiple times with increased retries
            current_video_file = get_latest_video(max_retries=5, delay=3)
            if not current_video_file:
                # One more attempt with longer delay
                log_with_task_details('WARNING', "First attempt to find video file failed, retrying...",
                    task_id=task_id)
                time.sleep(5)
                current_video_file = get_latest_video(max_retries=5, delay=3)
                
            if not current_video_file:
                error_msg = "No video file was generated"
                log_with_task_details('ERROR', error_msg,
                    task_id=task_id,
                    details=generation_details)
                update_task_status(task_id, 'failed', 'failed', None, conn)
                raise Exception(error_msg)

            # Get the full actual filename including extension with better error handling
            if original_filename:
                original_name = original_filename 
            else:
                original_name = os.path.basename(current_video_file)
                log_with_task_details('WARNING', "Using fallback filename from path",
                    task_id=task_id,
                    details={'fallback_name': original_name})
                    
            # Get just the name part for logging
            display_name = os.path.splitext(original_name)[0]
            log_with_task_details('INFO', f"Using generated video filename",
                task_id=task_id,
                details={'video_name': display_name, 'full_path': current_video_file})

            files_to_cleanup.add(current_video_file)

            # Apply utilities with improved error handling
            if task_data[3]:  # utilities JSON string
                try:
                    utilities = json.loads(task_data[3])
                    log_with_task_details('INFO', f"Processing {len(utilities)} utilities",
                        task_id=task_id,
                        details={'utility_count': len(utilities)})
                        
                    for index, util_id in enumerate(utilities):
                        c.execute("SELECT id, utility_curl, name FROM utilities WHERE id=?", (util_id,))
                        util = c.fetchone()
                        if not util:
                            log_with_task_details('WARNING', f"Utility ID {util_id} not found, skipping",
                                task_id=task_id)
                            continue
                            
                        utility_id, utility_curl, utility_name = util
                        log_with_task_details('INFO', f"Processing utility {index+1}/{len(utilities)}: {utility_name}",
                            task_id=task_id,
                            details={
                                'utility_id': utility_id,
                                'utility_name': utility_name,
                                'utility_curl': utility_curl
                            })

                        try:
                            # Verify the current video file exists and has proper size
                            if not os.path.exists(current_video_file):
                                log_with_task_details('ERROR', f"Video file not found for utility: {current_video_file}",
                                    task_id=task_id,
                                    details=generation_details)
                                    
                                # List all files to help debug
                                try:
                                    files = os.listdir('.')
                                    mp4_files = [f for f in files if f.lower().endswith('.mp4')]
                                    log_with_task_details('INFO', f"Looking for alternative files", 
                                        task_id=task_id,
                                        details={'all_files': files, 'mp4_files': mp4_files})
                                except Exception as list_e:
                                    log_with_task_details('WARNING', f"Error listing directory contents: {str(list_e)}", 
                                        task_id=task_id,
                                        details={'error': str(list_e)})
                                
                                # Try to get the latest video again with more retries
                                time.sleep(3)
                                latest = get_latest_video(max_retries=5, delay=2)
                                if latest:
                                    current_video_file = latest
                                    log_with_task_details('INFO', f"Retrieved alternative video file: {current_video_file}",
                                        task_id=task_id,
                                        details={'alternative_file': current_video_file})
                                else:
                                    log_with_task_details('ERROR', "Could not find video file for utility processing",
                                        task_id=task_id)
                                    raise Exception("Could not find video file for utility processing")
                            
                            # Additional validation on the file
                            file_size = os.path.getsize(current_video_file)
                            if file_size < 1024:  # Minimum 1KB
                                log_with_task_details('ERROR', f"Video file too small ({file_size} bytes)",
                                    task_id=task_id,
                                    details={'file_size': file_size, 'file_path': current_video_file})
                                raise Exception(f"Video file too small: {file_size} bytes")
                                
                            # Format the utility command - don't replace {input} yet
                            # Let execute_curl handle this with the proper path
                            util_cmd = utility_curl
                            
                            # Make sure the video file exists
                            if not os.path.exists(current_video_file):
                                log_with_task_details('ERROR', f"Video file not found for utility: {current_video_file}",
                                    task_id=task_id,
                                    details={'util_cmd': util_cmd})
                                raise Exception(f"Video file not found: {current_video_file}")
                            
                            # Get absolute path for the current video file
                            abs_video_file = os.path.abspath(current_video_file)
                            
                            # FIXED: Ensure the file is readable by the container user
                            try:
                                # Ensure file permissions are correct
                                os.chmod(current_video_file, 0o644)  # Make readable by all users
                                log_with_task_details('INFO', f"Set permissions on video file",
                                    task_id=task_id,
                                    details={'file_path': current_video_file})
                            except Exception as perm_e:
                                log_with_task_details('WARNING', f"Unable to set permissions: {str(perm_e)}",
                                    task_id=task_id,
                                    details={'error': str(perm_e)})
                            
                            log_with_task_details('INFO', f"Executing utility command",
                                task_id=task_id,
                                details={
                                    'utility_cmd': util_cmd,
                                    'video_file': current_video_file,
                                    'abs_path': abs_video_file
                                })
                                
                            # Allow time for file availability
                            time.sleep(2)
                            
                            # Execute the utility with more generous retry settings
                            success, stdout, stderr = execute_curl(
                                util_cmd, 
                                retries=3, 
                                retry_delay=5, 
                                validate_output=True, 
                                timeout=300,  # 5 minute timeout
                                mode='utility'
                            )
                            
                            if not success:
                                error_msg = f"Utility failed: {stderr}"
                                log_with_task_details('ERROR', error_msg,
                                    task_id=task_id,
                                    details={
                                        'stdout': stdout, 
                                        'stderr': stderr, 
                                        'utility_name': utility_name,
                                        **generation_details
                                    })
                                update_task_status(task_id, 'failed', 'failed', None, conn)
                                raise Exception(error_msg)
                            
                            # Allow more time for file operations to complete
                            time.sleep(3)
                            
                            # Check if the utility produced a new video file
                            latest_video = get_latest_video()
                            if latest_video and latest_video != current_video_file:
                                log_with_task_details('INFO', f"Utility produced new video file",
                                    task_id=task_id,
                                    details={
                                        'old_file': current_video_file,
                                        'new_file': latest_video
                                    })
                                    
                                files_to_cleanup.add(current_video_file)  # Add old file to cleanup
                                current_video_file = latest_video  # Update current file to new one
                                
                            log_with_task_details('INFO', f"Utility {utility_name} completed successfully",
                                task_id=task_id,
                                details={
                                    'utility_id': utility_id,
                                    'utility_name': utility_name
                                })
                            
                        except Exception as e:
                            # Log detailed error and continue with next utility if possible
                            log_with_task_details('ERROR', f"Utility {utility_name} failed: {str(e)}",
                                task_id=task_id,
                                details={
                                    'utility_id': utility_id,
                                    'utility_name': utility_name,
                                    'error': str(e)
                                })
                                
                            # If this is a critical error that should stop processing,
                            # clean up and exit with error
                            if "Could not find video file" in str(e) or "Video file too small" in str(e):
                                if current_video_file and os.path.exists(current_video_file):
                                    cleanup_video(current_video_file)
                                safe_rollback(conn, c)
                                update_task_status(task_id, 'failed', 'failed', None, conn)
                                raise
                                
                except json.JSONDecodeError as e:
                    log_with_task_details('ERROR', f"Failed to parse utilities JSON: {str(e)}",
                        task_id=task_id,
                        details={'utilities_json': task_data[3], 'error': str(e)})
                except Exception as e:
                    # Clean up on general utility processing failure
                    log_with_task_details('ERROR', f"Utility processing failed with error: {str(e)}",
                        task_id=task_id,
                        details={'error': str(e), **generation_details})
                    if current_video_file and os.path.exists(current_video_file):
                        cleanup_video(current_video_file)
                    safe_rollback(conn, c)
                    update_task_status(task_id, 'failed', 'failed', None, conn)
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
        if not preview_mode:
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

def process_video_upload(task_id, video_info=None, preview_mode=False, conn=None):
    """Handle video uploading to platforms"""
    upload_details = {
        'task_id': task_id,
        'mode': 'preview' if preview_mode else 'normal',
        'start_time': datetime.now().isoformat()
    }
    
    if video_info:
        upload_details['video_id'] = video_info[0]
        upload_details['scheduled_time'] = video_info[3]
    
    log_with_task_details('INFO', f"Starting video upload process",
        task_id=task_id,
        details=upload_details)
    
    files_to_cleanup = set()
    should_close_conn = conn is None
    
    try:
        # Create a new connection if one wasn't provided
        if conn is None:
            conn = db.get_connection()
            
        if conn is None:
            log_with_task_details('ERROR', "Failed to obtain database connection",
                task_id=task_id,
                details=upload_details)
            return False
            
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
            'original_name': original_name  # This is the key change - make sure we're using the database value
        }



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
            success, stdout, stderr = execute_curl(upload_cmd, retries=3, retry_delay=5, mode='uploader', timeout=600)  # 10 minute timeout
            current_stdout, current_stderr = stdout, stderr
            used_fallback = False
            fallback_level = 0  # 0 = no fallback, 1 = primary fallback, 2 = secondary fallback

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
                    
                success, stdout, stderr = execute_curl(fallback_cmd, retries=3, retry_delay=5, mode='uploader', timeout=600)  # 10 minute timeout
                if success:
                    used_fallback = True
                    fallback_level = 1
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
                    
                success, stdout, stderr = execute_curl(fallback_cmd_2, retries=3, retry_delay=5, mode='uploader', timeout=600)  # 10 minute timeout
                if success:
                    used_fallback = True
                    fallback_level = 2
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

        # Update video and task status with upload time
        c.execute("""
            UPDATE generated_videos 
            SET status = 'completed',
                upload_status = 'completed',
                uploaded_at = datetime('now')
            WHERE id = ?
        """, (video_id,))

        # Delete the processed video file after successful upload
        if os.path.exists(processed_path):
            try:
                os.remove(processed_path)
                log_with_task_details('INFO', f"Removed processed video after successful upload",
                    task_id=task_id,
                    details={'video_id': video_id, 'file_path': processed_path})
            except Exception as e:
                log_with_task_details('WARNING', f"Failed to remove processed video: {str(e)}",
                    task_id=task_id,
                    details={'error': str(e), 'video_id': video_id, 'file_path': processed_path})
        
        # Also clean up any platform-specific copies that might be left behind
        platform_suffixes = ['_YouTube', '_Instagram', '_TikTok', '_Twitter', '_Facebook']
        dir_name = os.path.dirname(processed_path)
        base_name, ext = os.path.splitext(os.path.basename(processed_path))
        
        for suffix in platform_suffixes:
            platform_file = os.path.join(dir_name, f"{base_name}{suffix}{ext}")
            if os.path.exists(platform_file):
                try:
                    os.remove(platform_file)
                    log_with_task_details('INFO', f"Cleaned up platform-specific video file",
                        task_id=task_id,
                        details={'file_path': platform_file})
                except Exception as e:
                    log_with_task_details('WARNING', f"Failed to remove platform-specific video: {str(e)}",
                        task_id=task_id,
                        details={'error': str(e), 'file_path': platform_file})
                
        # Clean up any temporary 'safe' files
        safe_files = [f for f in os.listdir(dir_name) if f.startswith('upload_') and f.endswith('.mp4')]
        for safe_file in safe_files:
            safe_path = os.path.join(dir_name, safe_file)
            try:
                os.remove(safe_path)
                log_with_task_details('INFO', f"Cleaned up temporary safe video file",
                    task_id=task_id,
                    details={'file_path': safe_path})
            except Exception as e:
                log_with_task_details('WARNING', f"Failed to remove temporary safe video: {str(e)}",
                    task_id=task_id,
                    details={'error': str(e), 'file_path': safe_path})

        # Check if this is the last pending video for the task
        c.execute("""
            SELECT COUNT(*) 
            FROM generated_videos 
            WHERE task_id = ? AND upload_status = 'pending'
        """, (task_id,))
        pending_count = c.fetchone()[0]

        if pending_count == 0:
            # Make sure we check for ALL pending videos, not just ones with current status
            c.execute("""
                SELECT COUNT(*) 
                FROM generated_videos 
                WHERE task_id = ? AND upload_status = 'pending'
                AND scheduled_time > datetime('now', 'localtime')
            """, (task_id,))
            future_pending_count = c.fetchone()[0]
            
            if future_pending_count == 0:
                # Only mark the task as fully completed when there are no more pending videos at all
                update_task_status(task_id, 'completed', 'completed', None, conn)
            else:
                # If there are still future scheduled videos, keep the task in progress
                logger.info(f"Task {task_id} has {future_pending_count} future videos scheduled for upload")
                update_task_status(task_id, 'pending', 'processed', None, conn)

        # Send email notification if enabled
        if task_data[4]:  # email_notify
            try:
                # Include more details in the notification
                platform_names = ', '.join(uploaded_platforms) if uploaded_platforms else 'None'
                log_with_task_details('INFO', f"Sending upload completion notification",
                    task_id=task_id,
                    details={
                        'email': task_data[4],
                        'platforms': platform_names,
                        'video_id': video_id
                    })
                    
                # Check if this is part of night processing (scheduled in advance)
                is_scheduled = False
                try:
                    if scheduled_time:
                        scheduled_dt = datetime.fromisoformat(scheduled_time)
                        is_scheduled = scheduled_dt > datetime.now() - timedelta(minutes=10)
                except Exception as sched_err:
                    log_with_task_details('WARNING', f"Error parsing scheduled time: {str(sched_err)}",
                        task_id=task_id,
                        details={'scheduled_time': scheduled_time, 'error': str(sched_err)})
                
                # Allow a small delay for database operations to complete
                time.sleep(1)
                
                try:
                    # Import at function level to avoid circular imports
                    from webapp.core_app.core.email_utils import send_task_completion_notification
                    
                    send_task_completion_notification(
                        task_id, 
                        task_data[0],  # task name
                        task_data[4],  # email address
                        success=True,
                        platforms=uploaded_platforms,
                        night_processing=is_scheduled  # Mark as night processing if scheduled
                    )
                    log_with_task_details('INFO', f"Successfully sent upload completion notification", task_id=task_id)
                except ImportError as ie:
                    log_with_task_details('ERROR', f"Email module import error: {str(ie)}", task_id=task_id)
                except Exception as email_e:
                    log_with_task_details('ERROR', f"Error sending completion notification: {str(email_e)}",
                        task_id=task_id,
                        details={'error': str(email_e)})
            except Exception as e:
                log_with_task_details('ERROR', f"Failed to prepare email notification",
                    task_id=task_id,
                    details={'error': str(e), **upload_details})

        return True

    except Exception as e:
        log_with_task_details('ERROR', f"Upload process failed: {str(e)}",
            task_id=task_id,
            details={'error': str(e), **upload_details})
        if not preview_mode:
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
        
        # Only close the connection if we created it
        if should_close_conn and conn:
            try:
                conn.close()
            except Exception as e:
                log_with_task_details('ERROR',
                    f"Failed to close database connection: {str(e)}",
                    task_id=task_id,
                    details={'error': str(e)})

def process_video_pipeline(task_id, schedule_time=None, preview_mode=False, parent_has_lock=False):
    """Main pipeline process that coordinates generation and upload"""
    pipeline_details = {
        'task_id': task_id,
        'mode': 'preview' if preview_mode else 'normal',
        'schedule_time': schedule_time.isoformat() if schedule_time else None,
        'start_time': datetime.now().isoformat(),
        'parent_has_lock': parent_has_lock
    }
    
    log_with_task_details('INFO', f"Starting video pipeline",
        task_id=task_id,
        details=pipeline_details)
    
    # Don't update status or acquire lock for previews
    if not preview_mode:
        update_task_status(task_id, 'running', 'pending')
    
    lock_acquired = False
    try:
        # Lock handling - only try to acquire if parent doesn't already have it
        if not preview_mode and not parent_has_lock:
            lock_acquired = check_and_set_lock(task_id)
            if not lock_acquired:
                log_with_task_details('INFO', 
                    "Another task is currently running. Task will retry later.",
                    task_id=task_id,
                    details=pipeline_details)
                return None
        
        with db.get_connection() as conn:
            # Night processing check
            if not preview_mode and not should_process_at_night():
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
                    conn,
                    parent_has_lock=parent_has_lock or lock_acquired  # Pass lock state to nested function
                )
            except Exception as e:
                log_with_task_details('ERROR', 
                    f"Video generation failed: {str(e)}",
                    task_id=task_id,
                    details={'error': str(e), **pipeline_details})
                raise
            
            if preview_mode:
                return generation_result[0] if isinstance(generation_result, tuple) else generation_result
            
            # Handle upload for normal runs
            if generation_result and isinstance(generation_result, tuple):
                video_path, video_id = generation_result
                pipeline_details['video_info'] = {
                    'path': video_path,
                    'id': video_id
                }
                
                # Immediate upload for manual runs, catchup, or fallback recovery
                # Manual run is indicated by current_app.manual_run flag or missing schedule time
                if not schedule_time or schedule_time <= datetime.now() or getattr(current_app, 'manual_run', False):
                    log_with_task_details('INFO', f"Executing immediate video upload" + 
                                               (" (manual run)" if getattr(current_app, 'manual_run', False) else ""),
                        task_id=task_id,
                        details={'manual_run': getattr(current_app, 'manual_run', False)})
                    
                    try:
                        # Fetch the correct original name from the database using video_id
                        c = conn.cursor()
                        c.execute("SELECT original_name FROM generated_videos WHERE id = ?", (video_id,))
                        original_name_result = c.fetchone()
                        if original_name_result and original_name_result[0]:
                            original_name = original_name_result[0]
                            log_with_task_details('INFO', f"Retrieved original name from database for upload",
                                task_id=task_id,
                                details={'original_name': original_name, 'video_id': video_id})
                        else:
                            # Fallback only if database lookup fails
                            original_name = os.path.basename(video_path)
                            log_with_task_details('WARNING', f"Failed to retrieve original name from database, using fallback",
                                task_id=task_id,
                                details={'fallback_name': original_name, 'video_id': video_id})
                        
                        upload_result = process_video_upload(
                            task_id, 
                            (video_id, original_name, video_path, datetime.now().isoformat()),
                            preview_mode, 
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
        if not preview_mode:
            update_task_status(task_id, 'failed', 'failed')
        raise
        
    finally:
        # Only release lock if we acquired it (not if parent had it)
        if lock_acquired and not parent_has_lock:
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

def check_for_missed_processing(force_process=False):
    """Check if any night processing was missed, typically after a restart. Now with improved scheduler job handling.
    
    Args:
        force_process: If True, will process regardless of time window
    """
    # Store original function to avoid recursive import issues
    _original_check_function = _check_for_missed_processing
    
    # Try to determine if we're in an app context
    try:
        from flask import current_app
        try:
            # Try to access current_app to check if we're in app context
            current_app._get_current_object()
            # We're in app context, call the original function directly
            return _original_check_function(force_process)
        except Exception:
            # Not in app context, create one
            try:
                # First try to get the app from our module
                from webapp.core_app import flask_app
                if flask_app:
                    with flask_app.app_context():
                        return _original_check_function(force_process)
                else:
                    # Fall back to direct import if app reference not available
                    from webapp.core_app import app 
                    with app.app_context():
                        return _original_check_function(force_process)
            except ImportError as e:
                logger.error(f"Failed to import app module: {e}")
                # Try a direct import as fallback
                try:
                    from webapp.core_app.core_app import app
                    with app.app_context():
                        return _original_check_function(force_process)
                except ImportError:
                    logger.error("Could not find Flask app, last resort attempt")
                    from flask import Flask
                    # Create a minimal temp app context
                    app = Flask("temp_app")
                    with app.app_context():
                        return _original_check_function(force_process)
    except Exception as e:
        logger.error(f"Error checking for missed processing (app context error): {str(e)}")
        return False

def _check_for_missed_processing(force_process=False):
    """Internal implementation for checking missed processing - always call through check_for_missed_processing"""
    try:
        today = datetime.now().date()
        yesterday = today - timedelta(days=1)
        yesterday_day = yesterday.strftime('%A')[:3].lower()
        
        # Get yesterday's date at start of night processing time
        night_hour, night_minute = map(int, os.getenv('NIGHT_PROCESSING_START', '01:30').split(':'))
        yesterday_night_time = datetime.combine(yesterday, datetime.min.time().replace(hour=night_hour, minute=night_minute))
        
        logger.info(f"Checking for missed night processing from {yesterday_night_time}")
        
        # Try to acquire lock
        lock_acquired = check_and_set_lock("missed_processing_check")
        if not lock_acquired and not force_process:
            logger.info("Could not acquire lock for missed processing check, will try later")
            return
            
        try:
            with db.get_connection() as conn:
                c = conn.cursor()
                
                # Find all tasks that should have run yesterday
                c.execute("""
                    SELECT id, schedule 
                    FROM tasks 
                    WHERE status != 'failed'
                    AND processing_status != 'failed'
                    AND schedule LIKE ?
                """, (f'%{yesterday_day}|%',))
                
                tasks = c.fetchall()
                processed_count = 0
                
                for task_id, schedule in tasks:
                    # Check if we have any generated videos from yesterday
                    c.execute("""
                        SELECT COUNT(*) FROM generated_videos
                        WHERE task_id = ?
                        AND DATE(generated_at) = DATE(?) 
                    """, (task_id, yesterday.strftime('%Y-%m-%d')))
                    
                    existing_count = c.fetchone()[0]
                    
                    # Also check today, as sometimes night processing happens right after midnight
                    c.execute("""
                        SELECT COUNT(*) FROM generated_videos
                        WHERE task_id = ?
                        AND DATE(generated_at) = DATE(?) 
                    """, (task_id, today.strftime('%Y-%m-%d')))
                    
                    today_count = c.fetchone()[0]
                    total_count = existing_count + today_count
                    
                    if total_count == 0 or force_process:
                        # No videos were generated yesterday or today for this task, generate now
                        log_with_task_details('INFO', 
                            f"Detected missed night processing for yesterday ({yesterday_day}) or startup recovery",
                            task_id=task_id)
                        
                        # Get today's schedule times for this task
                        today_schedules = get_next_day_schedules(schedule)
                        
                        if today_schedules:
                            # Process the task for today's schedules
                            try:
                                # Already in app context from outer function
                                from flask import current_app
                                current_app.manual_run = True  # Set manual flag to force processing
                                
                                for schedule_time in today_schedules:
                                    # IMPORTANT CHANGE: Pass the parent_lock to inform the pipeline that we already have the lock
                                    try:
                                        # Generate the video but don't try to acquire lock again
                                        with db.get_connection() as task_conn:
                                            video_result = process_video_generation(
                                                task_id, 
                                                schedule_time, 
                                                conn=task_conn,
                                                parent_has_lock=True  # Tell the function we already have the lock
                                            )
                                            
                                            if video_result and isinstance(video_result, tuple):
                                                video_path, video_id = video_result
                                                log_with_task_details('INFO', 
                                                    f"Successfully generated video for missed processing",
                                                    task_id=task_id,
                                                    details={
                                                        'video_path': video_path,
                                                        'video_id': video_id,
                                                        'schedule_time': schedule_time.isoformat() if schedule_time else None
                                                    })
                                                processed_count += 1
                                    except Exception as task_e:
                                        log_with_task_details('ERROR', 
                                            f"Failed to generate video for missed task: {str(task_e)}",
                                            task_id=task_id,
                                            details={'error': str(task_e)})
                                
                                current_app.manual_run = False  # Reset flag
                            except Exception as e:
                                log_with_task_details('ERROR', 
                                    f"Failed to process missed task {task_id}: {str(e)}",
                                    task_id=task_id)
                
                if processed_count > 0:
                    logger.info(f"Recovered {processed_count} missed video generations")
                else:
                    logger.info("No missed processing detected or recovery not needed")
                    
        finally:
            if lock_acquired:
                release_lock("missed_processing_check")
                
    except Exception as e:
        logger.error(f"Error checking for missed processing: {str(e)}")

def process_night_queue():
    """Process pending tasks during night window"""
    # First check if we need to recover from missed processing
    check_for_missed_processing()
    
    # Check if we should process at night
    in_night_window = should_process_at_night()
    if not in_night_window:
        logger.debug("Outside night processing window, skipping night queue")
        return
    
    logger.info("Starting night processing for scheduled tasks")

    lock_acquired = False
    task_summaries = []  # To store task results for notification
    total_videos_generated = 0
    notification_recipients = set()  # To collect all task email recipients
    
    try:
        # Try to acquire lock before processing
        if not check_and_set_lock("night_processing"):  # Using a consistent task_id for night processing
            logger.info("Could not acquire lock for night processing, will retry next cycle")
            return

        lock_acquired = True
        with db.get_connection() as conn:
            c = conn.cursor()
            
            # Get all tasks that need processing for tomorrow
            tomorrow = datetime.now() + timedelta(days=1)
            tomorrow_day = tomorrow.strftime('%A')[:3].lower()
            
            c.execute("""
                SELECT id, schedule, name, email_notify
                FROM tasks 
                WHERE status != 'failed'
                AND processing_status != 'failed'
                AND schedule LIKE ?
            """, (f'%{tomorrow_day}|%',))
            
            tasks = c.fetchall()
            logger.info(f"Found {len(tasks)} tasks scheduled for {tomorrow_day}")

            for task_id, schedule, task_name, email_notify in tasks:
                schedule_times = get_next_day_schedules(schedule)
                if not schedule_times:
                    logger.info(f"No schedule times found for task {task_id} ({task_name}) on {tomorrow_day}")
                    task_summaries.append({
                        'id': task_id,
                        'name': task_name,
                        'status': 'skipped',
                        'video_count': 0,
                        'error': 'No schedule times found'
                    })
                    continue
                
                # Add email recipient if notification is enabled
                if email_notify:
                    notification_recipients.add(email_notify)
                
                logger.info(f"Processing task {task_id} ({task_name}) with {len(schedule_times)} schedule time(s)")
                task_result = {
                    'id': task_id,
                    'name': task_name,
                    'status': 'success',
                    'video_count': 0,
                    'videos': []
                }
                
                # Ensure we have a proper application context for processing
                try:
                    # Check if we need to create an app context
                    from flask import current_app
                    in_app_context = False
                    
                    try:
                        # Try to access current_app to see if we're in an app context
                        current_app._get_current_object()
                        in_app_context = True
                    except Exception:
                        # We're not in an app context
                        in_app_context = False
                    
                    if in_app_context:
                        # Already in app context, proceed normally
                        current_app.manual_run = getattr(current_app, 'manual_run', False) or True
                        process_task_with_times(task_id, schedule_times, conn, task_result, total_videos_generated)
                        current_app.manual_run = False
                    else:
                        # Create an app context first
                        from webapp.core_app import app
                        with app.app_context():
                            app.manual_run = True  # For night processing to work in a context
                            process_task_with_times(task_id, schedule_times, conn, task_result, total_videos_generated) 
                            app.manual_run = False
                        
                except Exception as e:
                    error_msg = str(e)
                    log_with_task_details('ERROR', 
                        f"Night processing failed for task {task_id}: {error_msg}",
                        task_id=task_id,
                        details={'error': error_msg})
                    task_result['status'] = 'failed'
                    task_result['error'] = error_msg
                
                # Send individual task notifications if requested and videos were generated
                if email_notify and task_result['video_count'] > 0:
                    try:
                        from webapp.core_app.core.email_utils import send_task_completion_notification
                        send_task_completion_notification(
                            task_id,
                            task_name,
                            email_notify,
                            success=(task_result['status'] == 'success'),
                            night_processing=True
                        )
                    except Exception as e:
                        logger.error(f"Failed to send task completion notification: {str(e)}")
                
                # Add task result to summaries
                task_summaries.append(task_result)
            
            logger.info(f"Night processing queue completed: {total_videos_generated} videos generated")
            
            # Send comprehensive notification if we have recipients
            if notification_recipients and task_summaries:
                try:
                    from webapp.core_app.core.email_utils import send_night_processing_notification
                    # Convert set to comma-separated string
                    all_recipients = ','.join(notification_recipients)
                    
                    send_night_processing_notification(
                        task_summaries,
                        all_recipients
                    )
                    logger.info(f"Sent night processing summary notification to {len(notification_recipients)} recipients")
                except Exception as e:
                    logger.error(f"Failed to send night processing notification: {str(e)}")


    except Exception as e:
        logger.error(f"Error in night processing queue: {str(e)}")
        
    finally:
        if lock_acquired:
            try:
                release_lock("night_processing")
                logger.info("Released lock after night processing")
            except Exception as e:
                logger.error(f"Failed to release lock after night processing: {str(e)}")
                try:
                    force_release_lock()
                    logger.info("Force-released lock after night processing")
                except Exception as force_e:
                    logger.error(f"Force release also failed after night processing: {str(force_e)}")
def process_task_with_times(task_id, schedule_times, conn, task_result, total_videos_generated):
    """Helper function to process a task with its schedule times
    
    Created to be used with application context to avoid code duplication
    """
    for schedule_time in schedule_times:
        logger.info(f"Generating video for task {task_id} at {schedule_time}")
        
        # First clean up any existing temporary files to avoid confusion
        cleanup_existing_mp4s()
        
        # Process the video generation
        result = process_video_generation(task_id, schedule_time, conn=conn)
        
        # Verify that the result is valid
        if result and isinstance(result, tuple) and len(result) == 2:
            video_path, video_id = result
            logger.info(f"Successfully generated video for task {task_id} scheduled at {schedule_time}")
            logger.info(f"Video path: {video_path}, Video ID: {video_id}")
            
            # Track the video for notification
            task_result['video_count'] += 1
            total_videos_generated += 1
            task_result['videos'].append({
                'id': video_id,
                'path': video_path,
                'schedule_time': schedule_time.isoformat() if schedule_time else None
            })
            
            # Validate the generated video file
            if video_path and os.path.exists(video_path):
                is_valid, validation_msg = validate_video_file(video_path)
                if is_valid:
                    logger.info(f"Video file validated successfully: {video_path}")
                else:
                    logger.warning(f"Video file validation failed: {validation_msg} for {video_path}")
            else:
                logger.warning(f"Generated video file does not exist: {video_path}")
        else:
            logger.warning(f"Failed to generate video for task {task_id} scheduled at {schedule_time}")
            if not task_result.get('error'):
                task_result['error'] = 'Failed to generate video for some schedule times'

def process_scheduled_uploads():
    """Process tasks that are ready for upload - Modified to process ALL pending videos"""
    lock_acquired = False
    try:
        # Always log that we're checking for uploads
        logger.info("Checking for videos ready to upload...")
        
        if not check_and_set_lock():
            logger.debug("Task lock is held, skipping scheduled uploads")
            return
            
        lock_acquired = True
        
        # Check if we need to create an app context
        try:
            from flask import current_app
            in_app_context = False
            
            try:
                # Try to access current_app to see if we're in an app context
                current_app._get_current_object()
                in_app_context = True
            except Exception:
                # We're not in an app context
                in_app_context = False
                
            if in_app_context:
                # Already in app context, proceed normally
                process_uploads_with_context()
            else:
                # Create an app context first
                from webapp.core_app import app
                with app.app_context():
                    process_uploads_with_context()
        except Exception as e:
            logger.error(f"Error in scheduled uploads processor: {str(e)}")
            
    finally:
        # Release lock if acquired
        if lock_acquired:
            try:
                release_lock()
            except Exception as e:
                logger.error(f"Failed to release lock after scheduled uploads: {str(e)}")
                try:
                    force_release_lock()
                except Exception as force_e:
                    logger.error(f"Force release also failed after scheduled uploads: {str(force_e)}")
                    
def process_uploads_with_context():
    """Helper function to process uploads with proper context"""
    processed_count = 0
    error_count = 0
    
    with db.get_connection() as conn:
        c = conn.cursor()
        # Modified query to get ALL pending uploads for the current time
        # Removed LIMIT 1 to process all pending videos
        c.execute('''
            SELECT v.task_id, v.id, v.original_name, v.processed_path, v.scheduled_time
            FROM generated_videos v
            JOIN tasks t ON v.task_id = t.id
            WHERE v.upload_status = 'pending'
            AND v.scheduled_time <= datetime('now', 'localtime')
            AND t.status != 'failed'
            ORDER BY v.scheduled_time ASC
        ''')
        pending_uploads = c.fetchall()
        
        if pending_uploads:
            logger.info(f"Found {len(pending_uploads)} pending videos to upload")
            
            for pending_upload in pending_uploads:
                task_id, video_id, original_name, processed_path, scheduled_time = pending_upload
                try:
                    # Important: Don't let one failure prevent other uploads
                    process_video_upload(
                        task_id, 
                        (video_id, original_name, processed_path, scheduled_time), 
                        conn=conn
                    )
                    processed_count += 1
                    logger.info(f"Successfully processed upload for video {video_id}, task {task_id}")
                except Exception as e:
                    error_count += 1
                    log_with_task_details('ERROR', 
                        f"Failed to process upload for task {task_id}: {str(e)}",
                        task_id=task_id,
                        details={
                            'error': str(e),
                            'video_id': video_id,
                            'scheduled_time': scheduled_time
                        })
    
    # Log the summary of processed videos
    if processed_count > 0:
        logger.info(f"Scheduled uploads complete: Processed {processed_count} videos with {error_count} errors")

def cleanup_files(video_files):
    """Cleanup multiple video files with error handling"""
    for file in video_files:
        if file and os.path.exists(file):
            try:
                cleanup_video(file)
            except Exception as e:
                log_with_details('ERROR', f"Failed to cleanup file {file}: {str(e)}",
                    details={'file': file, 'error': str(e)})
    
    # Safety check - look for any temporary files that have been created in the current directory
    current_dir = os.getcwd()
    try:
        # Find any mp4 files in the current directory
        for file in os.listdir(current_dir):
            if file.endswith('.mp4'):
                file_path = os.path.join(current_dir, file)
                try:
                    cleanup_video(file_path)
                    log_with_details('INFO', f"Cleaned up additional mp4 file in current directory: {file}")
                except Exception as e:
                    log_with_details('WARNING', f"Failed to clean up additional mp4 file: {str(e)}",
                        details={'file': file, 'error': str(e)})
    except Exception as e:
        log_with_details('ERROR', f"Error searching for additional files to clean up: {str(e)}")
                    
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
        
        # Verify the connection is healthy
        if not check_connection_health(conn):
            logger.error("Failed to create a healthy database connection for force lock release")
            # If we can't get a good connection, we can't do much - consider the lock cleared anyway
            # because the next attempt will try again with a fresh connection
            return True
            
        c = conn.cursor()
        
        # Start transaction to prevent race conditions
        db.begin_transaction(conn)
        
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
        
        db.commit_transaction(conn)
        
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
                db.rollback_transaction(conn)
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