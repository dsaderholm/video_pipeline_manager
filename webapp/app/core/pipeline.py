import sqlite3
import json
import time
import os
import logging
import shutil
from datetime import datetime
from app import logger
from app.core.utils import execute_curl, get_latest_video, cleanup_video, format_upload_command, log_with_details
from app.core.email_utils import send_task_completion_notification
from flask import current_app

def log_with_task_details(level, message, task_id, details=None):
    """Helper function to log with task ID and structured details"""
    if details is None:
        details = {}
    details['task_id'] = task_id
    log_with_details(level, message, task_id=task_id, details=details, source='pipeline')
    
    # Also log to task-specific logger for console output
    logger_instance = logging.getLogger(f'task_{task_id}')
    logger_method = getattr(logger_instance, level.lower())
    logger_method(message)

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
    
    try:
        with sqlite3.connect('pipeline.db') as conn:
            c = conn.cursor()
            c.execute("""
                CREATE TABLE IF NOT EXISTS task_lock (
                    id INTEGER PRIMARY KEY,
                    locked INTEGER DEFAULT 0,
                    task_id INTEGER,
                    locked_at TIMESTAMP
                )
            """)
            conn.commit()
            
            c.execute("INSERT OR IGNORE INTO task_lock (id, locked) VALUES (1, 0)")
            conn.commit()
            
            c.execute("SELECT locked, task_id, locked_at FROM task_lock WHERE id = 1")
            current_lock = c.fetchone()
            lock_details['current_state'] = {
                'locked': bool(current_lock[0]),
                'task_id': current_lock[1],
                'locked_at': current_lock[2]
            }
            
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
            conn.commit()
            
            acquired = c.rowcount > 0
            lock_details['acquired'] = acquired
            
            if acquired:
                log_with_details('INFO', "Successfully acquired pipeline lock",
                    details=lock_details)
            else:
                log_with_details('INFO', "Failed to acquire pipeline lock - another task is running",
                    details=lock_details)
            
            return acquired
            
    except Exception as e:
        lock_details['error'] = str(e)
        log_with_details('ERROR', f"Error managing pipeline lock",
            details=lock_details)
        raise

def release_lock():
    """Release the task lock"""
    lock_details = {
        'operation': 'release',
        'timestamp': datetime.now().isoformat()
    }
    
    try:
        with sqlite3.connect('pipeline.db') as conn:
            c = conn.cursor()
            
            c.execute("SELECT locked, task_id, locked_at FROM task_lock WHERE id = 1")
            current_lock = c.fetchone()
            lock_details['previous_state'] = {
                'locked': bool(current_lock[0]),
                'task_id': current_lock[1],
                'locked_at': current_lock[2]
            }
            
            c.execute("""
                UPDATE task_lock 
                SET locked = 0, task_id = NULL, locked_at = NULL 
                WHERE id = 1
            """)
            conn.commit()
            
            lock_details['released'] = True
            log_with_details('INFO', "Released pipeline lock",
                details=lock_details)
            
    except Exception as e:
        lock_details['error'] = str(e)
        log_with_details('ERROR', f"Error releasing pipeline lock",
            details=lock_details)
        raise

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
    
    if not (dry_run or preview_mode) and not check_and_set_lock():
        log_with_task_details('INFO', "Another task is currently running. Task will retry later.",
            task_id=task_id,
            details=pipeline_details)
        return False if dry_run else None
    
    try:
        with sqlite3.connect('pipeline.db') as conn:
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
                        
                    success, stdout, stderr = execute_curl(task_data[-1], validate_output=True)
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
                        
                        success, stdout, stderr = execute_curl(util_cmd, validate_output=True)
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
                        
                    c.execute("UPDATE tasks SET status='completed' WHERE id=?", (task_id,))
                    conn.commit()
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
                    SELECT p.id, p.name, p.uploader_curl, tpa.account_name, p.default_hashtags
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
                    
                    for platform_data in platform_data_list:
                        platform_id, platform_name, upload_curl, account_name, default_hashtags = platform_data
                        
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
                        
                        # Create platform_dict for format_upload_command
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
                            raise Exception(error_msg)
                        
                        platform_details['safe_video_path'] = safe_video_path
                        
                        # Execute upload
                        success, stdout, stderr = execute_curl(upload_cmd)
                        
                        # Restore original filename after upload attempt
                        try:
                            if os.path.exists(safe_video_path):
                                os.rename(safe_video_path, current_video_file)
                                platform_details['filename_restored'] = True
                        except OSError as e:
                            error_msg = f"Failed to restore original filename: {e}"
                            platform_details['filename_restore_error'] = str(e)
                            log_with_task_details('ERROR', error_msg,
                                task_id=task_id,
                                details=platform_details)
                        
                        if not success:
                            error_msg = f"Upload to {platform_name} failed: {stderr}"
                            platform_details.update({
                                'stdout': stdout,
                                'stderr': stderr
                            })
                            log_with_task_details('ERROR', error_msg,
                                task_id=task_id,
                                details=platform_details)
                            raise Exception(error_msg)
                            
                        log_with_task_details('INFO', f"Successfully uploaded to platform",
                            task_id=task_id,
                            details=platform_details)
                
                # Update task status and send notification
                completion_details = {
                    **pipeline_details,
                    'status': 'completed'
                }
                
                c.execute("UPDATE tasks SET status='completed' WHERE id=?", (task_id,))
                conn.commit()
                completion_details['status_updated'] = True
                
                if task_data[9]:  # If email notifications are enabled
                    c.execute("SELECT id, name, email_notify FROM tasks WHERE id=?", (task_id,))
                    task_info = c.fetchone()
                    if task_info and task_info[2]:  # email_notify contains the email address
                        send_task_completion_notification(task_id, task_info[1], task_info[2], success=True)
                        completion_details['notification_sent'] = True
                    log_with_task_details('INFO', "Sent completion notification",
                        task_id=task_id,
                        details=completion_details)
                
                # Clean up video file unless it's a preview that we want to keep
                if current_video_file and not preview_mode:
                    cleanup_video(current_video_file)
                    if safe_video_path and os.path.exists(safe_video_path):
                        cleanup_video(safe_video_path)
                    completion_details['videos_cleaned'] = True
                    log_with_task_details('INFO', "Cleaned up temporary video files",
                        task_id=task_id,
                        details=completion_details)
                
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
                
                log_with_task_details('ERROR', f"Pipeline error",
                    task_id=task_id,
                    details=error_details)
                    
                # Clean up any remaining safe video path if exists
                if safe_video_path and os.path.exists(safe_video_path):
                    try:
                        os.rename(safe_video_path, current_video_file)
                        error_details['filename_restored'] = True
                    except OSError as restore_error:
                        error_details['filename_restore_error'] = str(restore_error)
                raise
                
            finally:
                # Always try to restore original filename if safe path exists
                if safe_video_path and os.path.exists(safe_video_path):
                    try:
                        os.rename(safe_video_path, current_video_file)
                    except OSError:
                        pass
    
    except Exception as e:
        error_details = {
            **pipeline_details,
            'error': str(e),
            'error_type': type(e).__name__
        }
        
        log_with_task_details('ERROR', f"Task failed",
            task_id=task_id,
            details=error_details)
        
        # Send failure notification if email notifications are enabled
        if not (dry_run or preview_mode):
            try:
                c.execute("SELECT id, name, email_notify FROM tasks WHERE id=?", (task_id,))
                task_info = c.fetchone()
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
        # Always release the lock for real runs
        if not (dry_run or preview_mode):
            release_lock()
            log_with_task_details('INFO', "Released pipeline lock",
                task_id=task_id,
                details=pipeline_details)