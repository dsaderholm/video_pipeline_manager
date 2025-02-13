import json
import time
import os
import sys
import logging
import shutil
import threading
from datetime import datetime, timedelta
import logging
logger = logging.getLogger('app')
from app.core.utils import execute_curl, get_latest_video, cleanup_video, format_upload_command, log_with_details
from app.core.email_utils import send_task_completion_notification
from app.core.database import db
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

def check_and_set_lock():
    """Check if any task is running and set lock if not"""
    lock_details = {
        'operation': 'check_and_set',
        'timestamp': datetime.now().isoformat()
    }
    
    with db_lock:  # Use thread lock for atomic operation
        try:
            with db.get_connection() as conn:
                c = conn.cursor()
                c.execute("""
                    UPDATE task_lock 
                    SET locked = 0, task_id = NULL, locked_at = NULL 
                    WHERE locked = 1 
                    AND datetime(locked_at, '+30 minutes') < datetime('now')
                """)
                
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
    """Release the task lock"""
    with db_lock:
        try:
            with db.get_connection() as conn:
                c = conn.cursor()
                c.execute("""
                    UPDATE task_lock 
                    SET locked = 0, task_id = NULL, locked_at = NULL 
                    WHERE id = 1
                """)
        except Exception as e:
            log_with_task_details('ERROR', f"Error releasing pipeline lock: {str(e)}", 
                task_id=None,
                details={'error': str(e)})

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

def store_generated_video(task_id, original_name, processed_path, scheduled_time, conn=None):
    """Store information about a newly generated video"""
    should_close_conn = conn is None
    try:
        if conn is None:
            with db.get_connection() as conn:        
            c = conn.cursor()
            c.execute('''
                INSERT INTO generated_videos 
                (task_id, original_name, processed_path, scheduled_time, status, upload_status)
                VALUES (?, ?, ?, ?, 'completed', 'pending')
            ''', (task_id, original_name, processed_path, scheduled_time))
            return c.lastrowid
    finally:
        if should_close_conn and conn:
            conn.__exit__(None, None, None)

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

def process_video_generation(task_id, schedule_time=None, preview_mode=False, dry_run=False):
    """Handle video generation and utility processing"""
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
    conn = None
    
    try:
        with db.get_connection() as conn:
            c = conn.cursor()
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

            success, stdout, stderr = execute_curl(task_data[-1], validate_output=True, mode='generator')
            if not success:
                error_msg = f"Generator failed: {stderr}"
                log_with_task_details('ERROR', error_msg,
                    task_id=task_id,
                    details={'stdout': stdout, 'stderr': stderr, **generation_details})
                update_task_status(task_id, 'failed', 'failed', None, conn)
                raise Exception(error_msg)

            current_video_file = get_latest_video()
            if not current_video_file:
                error_msg = "No video file was generated"
                log_with_task_details('ERROR', error_msg,
                    task_id=task_id,
                    details=generation_details)
                update_task_status(task_id, 'failed', 'failed', None, conn)
                raise Exception(error_msg)

            files_to_cleanup.add(current_video_file)

            # Apply utilities
            if task_data[3]:  # utilities JSON string
                utilities = json.loads(task_data[3])
                for util_id in utilities:
                    c.execute("SELECT utility_curl FROM utilities WHERE id=?", (util_id,))
                    util = c.fetchone()
                    if not util:
                        continue

                    util_cmd = util[0].format(input=current_video_file)
                    success, stdout, stderr = execute_curl(util_cmd, validate_output=True, mode='utility')
                    if not success:
                        error_msg = f"Utility failed: {stderr}"
                        log_with_task_details('ERROR', error_msg,
                            task_id=task_id,
                            details={'stdout': stdout, 'stderr': stderr, **generation_details})
                        update_task_status(task_id, 'failed', 'failed', None, conn)
                        raise Exception(error_msg)

            # Handle preview mode
            if preview_mode:
                preview_dir = os.path.join('static', 'previews')
                os.makedirs(preview_dir, exist_ok=True)
                preview_path = os.path.join(preview_dir, f'preview_task_{task_id}.mp4')
                
                if os.path.exists(preview_path):
                    os.remove(preview_path)
                
                shutil.copy2(current_video_file, preview_path)
                update_task_status(task_id, 'completed', None, None, conn)
                return preview_path

            # Store processed video
            os.makedirs('processed_videos', exist_ok=True)
            original_name = os.path.basename(current_video_file)
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
                    conn
                )
            else:
                video_id = store_generated_video(
                    task_id, 
                    original_name, 
                    permanent_path, 
                    datetime.now().isoformat(), 
                    conn
                )
            
            update_task_status(task_id, 'pending', 'processed', permanent_path, conn)
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
        log_with_task_details('ERROR', f"Video generation failed: {str(e)}",
            task_id=task_id,
            details={'error': str(e), **generation_details})
        if not (dry_run or preview_mode):
            update_task_status(task_id, 'failed', 'failed', None, conn)
        raise

    finally:
        cleanup_files(files_to_cleanup)
        if conn:
            conn.__exit__(None, None, None)

def process_video_upload(task_id, video_info=None, preview_mode=False, dry_run=False):
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
    
    conn = None
    files_to_cleanup = set()
    
    try:
        with db.get_connection() as conn:
            c = conn.cursor()
            
            # Get video to upload
            if video_info:
                video_id, original_name, processed_path, scheduled_time = video_info
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

            if not processed_path or not os.path.exists(processed_path):
                log_with_task_details('ERROR', f"Video file not found: {processed_path}",
                    task_id=task_id,
                    details=upload_details)
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
                    details=upload_details)
                return False

            task_dict = {
                'id': task_id,
                'name': task_data[0],
                'hashtags': task_data[1],
                'sound_name': task_data[2],
                'sound_volume': task_data[3],
                'original_name': original_name
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
                
                # Create a copy of the video for this platform
                platform_video_file = f"{os.path.splitext(processed_path)[0]}_{platform_name}.mp4"
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
                success, stdout, stderr = execute_curl(upload_cmd, mode='uploader')
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
                        
                    success, stdout, stderr = execute_curl(fallback_cmd, mode='uploader')
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
        if conn:
            conn.__exit__(None, None, None)

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
    
    if not (dry_run or preview_mode):
        update_task_status(task_id, 'running', 'pending')
    
    if not (dry_run or preview_mode) and not check_and_set_lock():
        log_with_task_details('INFO', "Another task is currently running. Task will retry later.",
            task_id=task_id,
            details=pipeline_details)
        return False if dry_run else None
    
    try:
        # For night processing, check if this is within the night window
        if not (preview_mode or dry_run) and not should_process_at_night():
            # If outside night window, only proceed if this is a manual run
            if not getattr(current_app, 'manual_run', False):
                log_with_task_details('INFO', "Outside night processing window, task will be processed later.",
                    task_id=task_id,
                    details=pipeline_details)
                return None
        
        # Generate video
        generation_result = process_video_generation(task_id, schedule_time, preview_mode, dry_run)
        
        if preview_mode:
            return generation_result[0] if isinstance(generation_result, tuple) else generation_result
            
        if dry_run:
            return True
            
        # If it's a normal run and generation succeeded, proceed with upload if scheduled
        if generation_result and isinstance(generation_result, tuple):
            video_path, video_id = generation_result
            
            # If this is for immediate upload (manual run or catchup)
            if not schedule_time or schedule_time <= datetime.now():
                return process_video_upload(task_id, (video_id, None, video_path, datetime.now().isoformat()))
            
            return True  # Video generated successfully, will be uploaded at scheduled time
            
        return None
        
    except Exception as e:
        log_with_task_details('ERROR', f"Pipeline failed: {str(e)}",
            task_id=task_id,
            details={'error': str(e), **pipeline_details})
        raise
        
    finally:
        if not (dry_run or preview_mode):
            release_lock()

def process_night_queue():
    """Process pending tasks during night window"""
    if not should_process_at_night():
        logger.debug("Outside night processing window, skipping night queue")
        return

    try:
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
            """, (f'%{tomorrow_day}|%',))  # Only get tasks scheduled for tomorrow
            
            tasks = c.fetchall()

        for task_id, schedule in tasks:
            # Get tomorrow's schedule times for this task
            schedule_times = get_next_day_schedules(schedule)
            
            if not schedule_times:
                continue
                
            try:
                if check_and_set_lock():
                    try:
                        # Generate a video for each scheduled time
                        for schedule_time in schedule_times:
                            process_video_generation(task_id, schedule_time)
                    finally:
                        release_lock()
            except Exception as e:
                log_with_task_details('ERROR', f"Night processing failed for task {task_id}: {str(e)}",
                    task_id=task_id)

    except Exception as e:
        logger.error(f"Error in night processing queue: {str(e)}")

def process_scheduled_uploads():
    """Process tasks that are ready for upload"""
    if not check_and_set_lock():
        logger.debug("Task lock is held, skipping scheduled uploads")
        return
        
    try:
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
                LIMIT 1  -- Process one at a time
            ''')
            pending_upload = c.fetchone()
            
            if pending_upload:
                task_id, video_id, original_name, processed_path, scheduled_time = pending_upload
                try:
                    process_video_upload(task_id, (video_id, original_name, processed_path, scheduled_time))
                except Exception as e:
                    logger.error(f"Failed to process upload for task {task_id}: {e}")
    finally:
        release_lock()

def cleanup_files(video_files):
    """Cleanup multiple video files with error handling"""
    for file in video_files:
        if file and os.path.exists(file):
            try:
                cleanup_video(file)
            except Exception as e:
                log_with_details('ERROR', f"Failed to cleanup file {file}: {str(e)}",
                    details={'file': file, 'error': str(e)})