from flask import jsonify, request, Blueprint, current_app, send_from_directory, send_file
import json
from datetime import datetime, timedelta
import logging
import os
import time
import glob
from webapp.core_app.timezone import localize_timestamp
from webapp.core_app.core.database import db

# Create blueprint and logger
tasks_bp = Blueprint('tasks', __name__)
logger = logging.getLogger(__name__)

def validate_schedule(schedule):
    """Validate a schedule string and return (is_valid, error_message)"""
    try:
        valid_days = {'sun', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat'}
        
        # Check if schedule is empty
        if not schedule.strip():
            return False, "Schedule cannot be empty"

        day_schedules = schedule.split(';')
        for day_schedule in day_schedules:
            if not day_schedule.strip():
                continue
                
            # Check day|time format
            if '|' not in day_schedule:
                return False, f"Invalid schedule format in '{day_schedule}'. Expected 'day|time'"
                
            day, times = day_schedule.split('|')
            
            # Validate day
            if day.lower() not in valid_days:
                return False, f"Invalid day '{day}'. Must be one of: {', '.join(valid_days)}"
                
            # Validate times
            for time in times.split(','):
                if not time.strip():
                    return False, "Empty time value found"
                    
                try:
                    hour, minute = map(int, time.strip().split(':'))
                    if not (0 <= hour <= 23 and 0 <= minute <= 59):
                        return False, f"Invalid time '{time}'. Hours must be 0-23, minutes must be 0-59"
                except ValueError:
                    return False, f"Invalid time format '{time}'. Expected HH:MM"

        return True, ""
        
    except Exception as e:
        return False, f"Invalid schedule format: {str(e)}"

def get_video_details(video_id, conn):
    """Get detailed video information"""
    with conn.cursor() as c:
        c.execute("""
            SELECT id, task_id, original_name, processed_path, scheduled_time, 
                status, upload_status, error_message, retry_count, generated_at
            FROM generated_videos
            WHERE id = %s
        """, (video_id,))
        video = c.fetchone()
        if not video:
            return None
            
        return {
            'id': video[0],
            'task_id': video[1],
            'original_name': video[2],
            'processed_path': video[3],
            'scheduled_time': localize_timestamp(video[4]),
            'status': video[5],
            'upload_status': video[6],
            'error_message': video[7],
            'retry_count': video[8],
            'generated_at': localize_timestamp(video[9])
        }

def get_task_details(task_id, conn):
    """Get detailed task information including platforms, utilities, and videos"""
    with conn.cursor() as c:
        # Get basic task info with generator name
        c.execute("""
            SELECT t.*, g.name as generator_name
            FROM tasks t
            LEFT JOIN generators g ON t.generator_id = g.id
            WHERE t.id = %s
        """, (task_id,))
        task = c.fetchone()
        
        if not task:
            return None
            
        # Get utilities
        utilities = []
        if task[3]:  # utilities column
            utility_ids = json.loads(task[3])
            if utility_ids:
                placeholders = ', '.join(['%s'] * len(utility_ids))
                c.execute(f"SELECT id, name FROM utilities WHERE id IN ({placeholders})", utility_ids)
                utilities = [{'id': u[0], 'name': u[1]} for u in c.fetchall()]

        # Get platforms with account names
        c.execute("""
            SELECT p.id, p.name, p.uploader_curl, p.fallback_curl, p.fallback_curl_2,
                p.default_hashtags, tpa.account_name
            FROM platforms p
            JOIN task_platform_accounts tpa ON p.id = tpa.platform_id
            WHERE tpa.task_id = %s
        """, (task_id,))
        platforms = c.fetchall()

        # Get generated videos
        c.execute("""
            SELECT id FROM generated_videos 
            WHERE task_id = %s 
            ORDER BY scheduled_time DESC
        """, (task_id,))
        video_ids = [row[0] for row in c.fetchall()]
    videos = []
    for vid_id in video_ids:
        video_detail = get_video_details(vid_id, conn)
        if video_detail:
            videos.append(video_detail)

    return {
        'id': task[0],
        'name': task[1],
        'generator_id': task[2],
        'utilities': utilities,
        'schedule': task[4],
        'hashtags': task[5],
        'sound_name': task[6],
        'sound_volume': task[7],
        'status': task[8],
        'email_notify': task[9],
        'retry_count': task[10],
        'processing_status': task[11],
        'processed_video_path': task[12],
        'created_at': localize_timestamp(task[13]),
        'generator_name': task[14] if len(task) > 14 else '[Generator]',  # Fixed column index for generator_name
        'videos': videos,
        'platforms': [{
            'id': p[0],
            'name': p[1],
            'uploader_curl': p[2],
            'fallback_curl': p[3],
            'fallback_curl_2': p[4],
            'default_hashtags': p[5],
            'account_name': p[6]
        } for p in platforms]
    }

@tasks_bp.route('/api/tasks', methods=['GET'])
def get_tasks():
    with db.get_connection() as conn:
        tasks = []
        with conn.cursor() as c:
            # First, clean up any obviously stuck tasks
            c.execute("""
                UPDATE tasks 
                SET status = 'pending', processing_status = 'pending'
                WHERE status IN ('previewing', 'running') 
                AND NOT EXISTS (
                    SELECT 1 FROM task_lock 
                    WHERE locked = 1 AND task_id = tasks.id
                )
            """)
            
            stuck_reset_count = c.rowcount
            if stuck_reset_count > 0:
                logger.info(f"Auto-reset {stuck_reset_count} stuck tasks during task list fetch")
            
            # Get tasks with lock information
            c.execute("""
                SELECT t.*, g.name as generator_name, 
                       tl.locked, tl.task_id as lock_task_id
                FROM tasks t
                LEFT JOIN generators g ON t.generator_id = g.id
                LEFT JOIN task_lock tl ON tl.id = 1
                ORDER BY t.created_at DESC
            """)
            task_rows = c.fetchall()
            
            for task in task_rows:
                task_detail = get_task_details(task[0], conn)
                if task_detail:
                    # Correct the status based on actual lock state
                    locked = task[-2]  # second to last column
                    lock_task_id = task[-1]  # last column
                    
                    if locked and lock_task_id == task[0]:  # task[0] is task ID
                        task_detail['status'] = 'running'
                        task_detail['processing_status'] = 'processing'
                    elif task_detail['status'] == 'previewing' and (not locked or lock_task_id != task[0]):
                        task_detail['status'] = 'pending'
                        task_detail['processing_status'] = 'pending'
                    
                    tasks.append(task_detail)
                    
            conn.commit()
            return jsonify(tasks)

@tasks_bp.route('/api/tasks/<int:id>/videos', methods=['GET'])
def get_task_videos(id):
    """Get all videos for a specific task"""
    with db.get_connection() as conn:
        with conn.cursor() as c:
            c.execute("""
                SELECT id FROM generated_videos 
                WHERE task_id = %s 
                ORDER BY scheduled_time DESC
            """, (id,))
            video_ids = [row[0] for row in c.fetchall()]
            
            videos = []
            for vid_id in video_ids:
                video_detail = get_video_details(vid_id, conn)
                if video_detail:
                    videos.append(video_detail)
                    
            return jsonify(videos)

@tasks_bp.route('/api/tasks/<int:id>/videos/<int:video_id>', methods=['GET'])
def get_task_video(id, video_id):
    """Get details for a specific video"""
    with db.get_connection() as conn:
        video_detail = get_video_details(video_id, conn)
        if video_detail and video_detail['task_id'] == id:
            return jsonify(video_detail)
        return jsonify({'success': False, 'message': 'Video not found'}), 404

def retry_with_backoff(task_id, video_id=None):
    """Retry task or specific video with exponential backoff"""
    scheduler = current_app.scheduler
    
    with db.get_connection() as conn:
        with conn.cursor() as c:
            if video_id:
                # Retry specific video
                c.execute("SELECT retry_count FROM generated_videos WHERE id = %s", (video_id,))
                result = c.fetchone()
                retry_count = result[0] if result and result[0] is not None else 0
                
                if retry_count >= 3:
                    c.execute("""
                        UPDATE generated_videos 
                        SET status = 'failed', 
                            upload_status = 'failed',
                            retry_count = 0
                        WHERE id = %s
                    """, (video_id,))
                    conn.commit()
                    return
                    
                delay = 60 * (2 ** retry_count)
                next_run = datetime.now() + timedelta(seconds=delay)
                
                from webapp.core_app.core.pipeline import process_video_upload
                scheduler.add_job(
                    func=process_video_upload,
                    trigger='date',
                    run_date=next_run,
                    args=[task_id, video_id],
                    id=f'retry_video_{video_id}_{next_run.timestamp()}'
                )
                
                c.execute("""
                    UPDATE generated_videos 
                    SET retry_count = %s,
                        status = 'retrying',
                        upload_status = 'pending'
                    WHERE id = %s
                """, (retry_count + 1, video_id))
                
            else:
                # Retry entire task
                c.execute("SELECT retry_count FROM tasks WHERE id = %s", (task_id,))
                result = c.fetchone()
                retry_count = result[0] if result and result[0] is not None else 0
                
                if retry_count >= 3:
                    c.execute("""
                        UPDATE tasks 
                        SET status = 'failed', 
                            retry_count = 0,
                            processing_status = 'failed'
                        WHERE id = %s
                    """, (task_id,))
                    conn.commit()
                    return
                
                delay = 60 * (2 ** retry_count)
                next_run = datetime.now() + timedelta(seconds=delay)
                
                from webapp.core_app.core.pipeline import process_video_pipeline
                scheduler.add_job(
                    func=process_video_pipeline,
                    trigger='date',
                    run_date=next_run,
                    args=[task_id],
                    id=f'retry_task_{task_id}_{next_run.timestamp()}'
                )
                
                c.execute("""
                    UPDATE tasks 
                    SET retry_count = %s,
                        status = 'retrying',
                        processing_status = 'pending'
                    WHERE id = %s
                """, (retry_count + 1, task_id))
            
        conn.commit()

def cleanup_preview_dir():
    """Perform maintenance on the preview directory"""
    try:
        if not should_cleanup():
            logger.debug("Skipping cleanup - last cleanup was too recent")
            return

        from webapp.core_app.core.pipeline import get_preview_dir
        preview_dir = get_preview_dir()
        
        for file_path in glob.glob(os.path.join(preview_dir, 'preview_task_*.mp4')):
            try:
                mtime = datetime.fromtimestamp(os.path.getmtime(file_path))
                if datetime.now() - mtime > timedelta(hours=24):
                    try:
                        os.remove(file_path)
                        logger.info(f"Cleaned up old preview file: {file_path}")
                    except OSError as e:
                        logger.warning(f"Failed to delete old preview file {file_path}: {e}")
            except OSError as e:
                logger.warning(f"Error checking preview file {file_path}: {e}")
        
        marker_file = os.path.join(preview_dir, '.last_cleanup')
        with open(marker_file, 'w') as f:
            f.write(datetime.now().isoformat())
                    
    except Exception as e:
        logger.error(f"Error during preview directory maintenance: {e}")

def should_cleanup():
    """Check if cleanup is needed (avoid cleaning up too frequently)"""
    try:
        from webapp.core_app.core.pipeline import get_preview_dir
        preview_dir = get_preview_dir()
        marker_file = os.path.join(preview_dir, '.last_cleanup')
        
        if not os.path.exists(marker_file):
            return True
            
        last_cleanup = datetime.fromtimestamp(os.path.getmtime(marker_file))
        return datetime.now() - last_cleanup > timedelta(hours=1)
        
    except Exception:
        return True

@tasks_bp.route('/api/tasks', methods=['POST'])
def create_task():
    """Create a new video processing task with improved scheduler job handling"""
    
    data = request.json
    scheduler = current_app.scheduler
    
    # Validate schedule format
    is_valid, error_message = validate_schedule(data['schedule'])
    if not is_valid:
        return jsonify({'success': False, 'message': error_message}), 400
    
    with db.get_connection() as conn:
        try:
            # Begin transaction
            with conn.cursor() as c:
                # Insert the main task
                utilities_json = json.dumps(data.get('utilities', []))
                c.execute('''INSERT INTO tasks 
                            (name, generator_id, utilities, schedule, 
                             hashtags, sound_name, sound_volume, email_notify, retry_count,
                             processing_status)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 0, 'pending')
                            RETURNING id''',
                         (data['name'], data['generator_id'], utilities_json,
                          data['schedule'], data.get('hashtags'),
                          data.get('sound_name'), data.get('sound_volume', 'background'),
                          data.get('email_notify')))
                task_id = c.fetchone()[0]
                
                # Insert platform associations
                for platform in data.get('platforms', []):
                    c.execute('''INSERT INTO task_platform_accounts 
                                (task_id, platform_id, account_name)
                                VALUES (%s, %s, %s)''',
                             (task_id, platform['id'], platform['account_name']))
            
            # Add night processing schedule - runs at configured start time every night
            night_job_id = f'task_{task_id}_night_processing'
            night_hour, night_minute = os.getenv('NIGHT_PROCESSING_START', '22:00').split(':')
            
            # First check if the job already exists and remove it
            try:
                scheduler.remove_job(night_job_id)
                logger.info(f"Removed existing night processing job {night_job_id}")
            except:
                # Job doesn't exist yet, that's okay
                pass
                
            # Now create the job
            try:
                scheduler.add_job(
                    func='webapp.core_app.core.pipeline:process_night_queue',
                    trigger='cron',
                    hour=int(night_hour),
                    minute=int(night_minute),
                    id=night_job_id,
                    misfire_grace_time=3600,  # 1 hour grace time for night processing
                    replace_existing=True  # Ensure it replaces any existing job with same ID
                )
                logger.info(f"Scheduled night processing for task {task_id} at {night_hour}:{night_minute}")
            except Exception as e:
                logger.error(f"Failed to schedule night processing for task {task_id}: {str(e)}")
                # Continue with task creation even if scheduling fails
            
            # Schedule the upload jobs
            from webapp.core_app.core.pipeline import process_video_upload
            
            # Parse schedule for uploads
            day_schedules = data['schedule'].split(';')
            for day_schedule in day_schedules:
                if not day_schedule.strip():
                    continue
                    
                day, times = day_schedule.split('|')
                day_number = {
                    'sun': 0, 'mon': 1, 'tue': 2, 'wed': 3, 
                    'thu': 4, 'fri': 5, 'sat': 6
                }[day.lower()]
                
                # Schedule each upload time slot
                for time in times.split(','):
                    hour, minute = map(int, time.strip().split(':'))
                    
                    # Schedule the upload job
                    upload_job_id = f'task_{task_id}_{day}_{hour}_{minute}'
                    
                    # Try to remove any existing job first to avoid conflicts
                    try:
                        scheduler.remove_job(upload_job_id)
                        logger.info(f"Removed existing upload job {upload_job_id}")
                    except:
                        # Job doesn't exist yet, that's okay
                        pass
                    
                    try:
                        scheduler.add_job(
                            func=process_video_upload,
                            trigger='cron',
                            day_of_week=day_number,
                            hour=hour,
                            minute=minute,
                            args=[task_id],  # process_video_upload will find the correct video for this time
                            id=upload_job_id,
                            misfire_grace_time=300,  # 5 minutes grace time for uploads
                            replace_existing=True  # Ensure it replaces any existing job with same ID
                        )
                        logger.info(f"Scheduled upload for task {task_id} on {day} at {hour:02d}:{minute:02d}")
                    except Exception as e:
                        logger.error(f"Error scheduling upload for task {task_id} on {day} at {hour:02d}:{minute:02d}: {str(e)}")

            conn.commit()
            return jsonify({'id': task_id, 'status': 'scheduled'})
            
        except Exception as e:
            conn.rollback()
            logger.error(f"Error creating task: {str(e)}")
            return jsonify({'success': False, 'message': str(e)}), 500

@tasks_bp.route('/api/tasks/<int:id>', methods=['DELETE'])
def delete_task(id):
    scheduler = current_app.scheduler
    
    with db.get_connection() as conn:
        try:
            with conn.cursor() as begin_cursor:
                begin_cursor.execute('BEGIN')
            c = conn.cursor()
            
            # Remove scheduled jobs
            c.execute('SELECT schedule FROM tasks WHERE id = %s', (id,))
            task = c.fetchone()
            if task and task[0]:
                day_schedules = task[0].split(';')
                for day_schedule in day_schedules:
                    if not day_schedule.strip():
                        continue
                    
                    day, times = day_schedule.split('|')
                    for time in times.split(','):
                        hour, minute = map(int, time.strip().split(':'))
                        try:
                            scheduler.remove_job(f'task_{id}_{day}_{hour}_{minute}')
                        except:
                            pass

                # Remove night processing job
                try:
                    night_job_id = f'task_{id}_night_processing'
                    scheduler.remove_job(night_job_id)
                    logger.info(f"Removed night processing job {night_job_id}")
                except Exception as e:
                    logger.warning(f"Failed to remove night processing job for task {id}: {str(e)}")

                # Remove retry jobs
                for job in scheduler.get_jobs():
                    if job.id.startswith(f'retry_task_{id}') or job.id.startswith(f'retry_video_{id}'):
                        try:
                            scheduler.remove_job(job.id)
                        except:
                            pass
            
            # Delete generated videos
            c.execute("""
                SELECT processed_path FROM generated_videos
                WHERE task_id = %s
            """, (id,))
            video_paths = [row[0] for row in c.fetchall()]
            
            for path in video_paths:
                if path and os.path.exists(path):
                    try:
                        os.remove(path)
                    except Exception as e:
                        logger.warning(f"Failed to delete video file {path}: {e}")
            
            # Delete task platform associations and videos (will cascade)
            c.execute('DELETE FROM task_platform_accounts WHERE task_id = %s', (id,))
            c.execute('DELETE FROM generated_videos WHERE task_id = %s', (id,))
            
            # Delete the task
            c.execute('DELETE FROM tasks WHERE id = %s', (id,))
            
            conn.commit()
            return jsonify({'success': True})
            
        except Exception as e:
            conn.rollback()
            logger.error(f"Error deleting task: {str(e)}")
            return jsonify({'success': False, 'message': str(e)}), 500

@tasks_bp.route('/api/tasks/<int:id>/run', methods=['POST'])
def run_task(id):
    try:
        # Set flag for manual run
        setattr(current_app, 'manual_run', True)
        
        # First verify task exists and is not already running
        with db.get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT id, status FROM tasks WHERE id = %s", (id,))
            task = c.fetchone()
            if not task:
                return jsonify({
                    'success': False,
                    'message': f'Task {id} not found'
                }), 404

            # Check for stale lock and clear it
            c.execute("""
                UPDATE task_lock 
                SET locked = 0, 
                    task_id = NULL, 
                    locked_at = NULL 
                WHERE id = 1 
                AND (
                    locked_at IS NULL 
                    OR locked_at + interval '5 minutes' < now()
                )
            """)
            
            # Check if task is locked
            c.execute("SELECT locked, task_id FROM task_lock WHERE id = 1")
            lock_status = c.fetchone()
            if lock_status and lock_status[0] == 1:
                return jsonify({
                    'success': False,
                    'message': f'Task {id} is already running'
                }), 409
                
            conn.commit()

        from webapp.core_app.core.pipeline import process_video_pipeline
        logger.info(f"Starting video pipeline for task {id}")
        process_video_pipeline(id)
        
        # Reset manual run flag
        setattr(current_app, 'manual_run', False)
        
        return jsonify({
            'success': True,
            'message': 'Task started successfully'
        })
        
    except Exception as e:
        logger.error(f"Error running task {id}: {str(e)}")
        # Reset manual run flag
        setattr(current_app, 'manual_run', False)
        # If task fails, force release the lock
        from webapp.core_app.core.pipeline import force_release_lock
        force_release_lock()
        return jsonify({
            'success': False,
            'message': str(e)
        }), 500

@tasks_bp.route('/api/tasks/<int:id>/preview', methods=['POST'])
def preview_task(id):
    try:
        # Run cleanup before generating new preview
        cleanup_preview_dir()
        
        with db.get_connection() as conn:
            c = conn.cursor()
            
            # First, check if task is stuck in previewing state and reset it
            c.execute("""
                UPDATE tasks 
                SET status = 'pending', processing_status = 'pending'
                WHERE id = %s AND status = 'previewing'
            """, (id,))
            reset_count = c.rowcount
            
            if reset_count > 0:
                logger.info(f"Reset stuck preview state for task {id}")
            
            # Now check this specific task
            c.execute("SELECT id, status, processing_status FROM tasks WHERE id = %s", (id,))
            task = c.fetchone()
            
            if not task:
                return jsonify({
                    'success': False,
                    'message': f'Task {id} not found'
                }), 404
                
            # Only block if task is actually running (not stuck in previewing)
            if task[1] == 'running' or task[2] == 'processing':
                return jsonify({
                    'success': False,
                    'message': 'Task is currently running or processing'
                }), 409

            # Clean up any existing preview files for this task
            from webapp.core_app.core.pipeline import get_preview_dir
            preview_dir = get_preview_dir()
            preview_path = os.path.join(preview_dir, f'preview_task_{id}.mp4')
            
            max_retries = 3
            retry_delay = 1  # second
            
            for attempt in range(max_retries):
                try:
                    if os.path.exists(preview_path):
                        os.remove(preview_path)
                    break
                except (IOError, PermissionError) as e:
                    if attempt == max_retries - 1:  # Last attempt
                        logger.error(f"Failed to remove existing preview after {max_retries} attempts: {e}")
                        raise
                    time.sleep(retry_delay)

            # Update task status for preview
            c.execute("""
                UPDATE tasks 
                SET status = 'previewing',
                    processing_status = 'pending'
                WHERE id = %s
            """, (id,))
            
            conn.commit()

        # Start the pipeline in preview mode
        from webapp.core_app.core.pipeline import process_video_pipeline
        preview_result = process_video_pipeline(id, preview_mode=True)
        
        if preview_result:
            # Verify file is completely written
            file_size = 0
            prev_size = -1
            max_wait = 10  # seconds
            start_time = time.time()
            
            while file_size != prev_size and time.time() - start_time < max_wait:
                prev_size = file_size
                try:
                    file_size = os.path.getsize(preview_path)
                    time.sleep(0.5)
                except OSError:
                    time.sleep(0.5)
                    continue
            
            if time.time() - start_time >= max_wait:
                raise Exception("Timeout waiting for preview file to stabilize")

            # Only proceed if file exists and is stable
            if os.path.exists(preview_path) and file_size > 0:
                with db.get_connection() as conn:
                    c = conn.cursor()
                    c.execute("""
                        UPDATE tasks 
                        SET status = 'completed',
                            processing_status = 'completed'
                        WHERE id = %s
                    """, (id,))
                    conn.commit()
                    
                return jsonify({
                    'success': True,
                    'message': 'Preview generated successfully',
                    'preview_url': f'/static/previews/preview_task_{id}.mp4'
                })
            else:
                raise Exception("Preview file not found or empty")
                
    except Exception as e:
        logger.error(f"Error generating preview for task {id}: {str(e)}")
        # Update task status on error - reset to pending instead of failed
        with db.get_connection() as conn:
            c = conn.cursor()
            c.execute("""
                UPDATE tasks 
                SET status = 'pending',
                    processing_status = 'pending'
                WHERE id = %s
            """, (id,))
            conn.commit()
            
        return jsonify({
            'success': False,
            'message': f'Preview generation failed: {str(e)}. Task status has been reset.'
        }), 500

@tasks_bp.route('/api/tasks/<int:id>/preview/download', methods=['GET'])
def download_preview(id):
    try:
        from webapp.core_app.core.pipeline import get_preview_dir
        preview_dir = get_preview_dir()
        preview_path = os.path.join(preview_dir, f'preview_task_{id}.mp4')
        
        if not os.path.exists(preview_path):
            return jsonify({
                'success': False,
                'message': 'Preview not found or still generating'
            }), 404

        try:
            # Ensure file is ready for reading
            max_retries = 10
            retry_delay = 1  # second
            for attempt in range(max_retries):
                try:
                    with open(preview_path, 'rb') as f:
                        # Try to read a small chunk to verify file access
                        f.read(1024)
                        f.seek(0)
                        break
                except (IOError, PermissionError):
                    if attempt < max_retries - 1:
                        time.sleep(retry_delay)
                        continue
                    raise
            
            return send_file(
                preview_path,
                mimetype='video/mp4',
                as_attachment=True,
                download_name=f'preview_task_{id}.mp4'
            )
            
        except Exception as e:
            logger.error(f"Error sending preview file: {e}")
            raise
            
        finally:
            # Clean up the preview file after sending
            cleanup_preview_dir()
                
    except Exception as e:
        logger.error(f"Error downloading preview for task {id}: {str(e)}")
        return jsonify({
            'success': False,
            'message': f'Error downloading preview: {str(e)}'
        }), 500



@tasks_bp.route('/api/tasks/<int:id>/generate', methods=['POST'])
def generate_task(id):
    """Manual generation of video content regardless of schedule"""
    try:
        # Set flag for manual run
        setattr(current_app, 'manual_run', True)
        
        with db.get_connection() as conn:
            c = conn.cursor()
            c.execute("SELECT id, status, processing_status, schedule FROM tasks WHERE id = %s", (id,))
            task = c.fetchone()
            
            if not task:
                return jsonify({
                    'success': False,
                    'message': f'Task {id} not found'
                }), 404
            
            if task[1] == 'running' or task[2] == 'processing':
                return jsonify({
                    'success': False,
                    'message': f'Task {id} is already being processed'
                }), 409

            # Get next scheduled time if exists
            schedule = task[3]
            next_time = None
            if schedule:
                day_schedules = schedule.split(';')
                now = datetime.now()
                for day_schedule in day_schedules:
                    if not day_schedule.strip():
                        continue
                    day, times = day_schedule.split('|')
                    for time_str in times.split(','):
                        hour, minute = map(int, time_str.strip().split(':'))
                        schedule_time = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                        if schedule_time > now:
                            if not next_time or schedule_time < next_time:
                                next_time = schedule_time

        from webapp.core_app.core.pipeline import process_video_generation
        result = process_video_generation(id, next_time if next_time else None)
        
        # Reset manual run flag
        setattr(current_app, 'manual_run', False)
        
        if result and isinstance(result, tuple):
            video_path, video_id = result
            return jsonify({
                'success': True,
                'message': 'Video generation completed successfully',
                'video_path': video_path,
                'video_id': video_id
            })
        else:
            return jsonify({
                'success': False,
                'message': 'Video generation failed'
            }), 500
            
    except Exception as e:
        logger.error(f"Error generating video for task {id}: {str(e)}")
        # Reset manual run flag
        setattr(current_app, 'manual_run', False)
        return jsonify({
            'success': False,
            'message': str(e)
        }), 500

@tasks_bp.route('/api/tasks/<int:id>/videos/<int:video_id>/upload', methods=['POST'])
def upload_video(id, video_id):
    """Manual upload of a specific video"""
    try:
        with db.get_connection() as conn:
            c = conn.cursor()
            c.execute("""
                SELECT v.id, v.original_name, v.processed_path, v.scheduled_time,
                       t.status as task_status
                FROM generated_videos v
                JOIN tasks t ON v.task_id = t.id
                WHERE v.task_id = %s AND v.id = %s
            """, (id, video_id))
            video = c.fetchone()
            
            if not video:
                return jsonify({
                    'success': False,
                    'message': f'Video {video_id} not found for task {id}'
                }), 404
            
            if video[4] == 'running':
                return jsonify({
                    'success': False,
                    'message': f'Task {id} is currently running'
                }), 409

            if not video[2] or not os.path.exists(video[2]):
                return jsonify({
                    'success': False,
                    'message': f'Video file not found'
                }), 404

        from webapp.core_app.core.pipeline import process_video_upload
        success = process_video_upload(id, (video_id, video[1], video[2], video[3]))
        
        if success:
            return jsonify({
                'success': True,
                'message': 'Upload completed successfully'
            })
        else:
            return jsonify({
                'success': False,
                'message': 'Upload failed'
            }), 500
            
    except Exception as e:
        logger.error(f"Error uploading video {video_id} for task {id}: {str(e)}")
        return jsonify({
            'success': False,
            'message': str(e)
        }), 500

@tasks_bp.route('/api/tasks/recover-missed', methods=['POST'])
def recover_missed_processing():
    """Force check and recovery of any missed night processing"""
    try:
        # Set flag for manual run to ensure uploads happen
        setattr(current_app, 'manual_run', True)
        
        from webapp.core_app.core.pipeline import check_for_missed_processing
        check_for_missed_processing(force_process=True)
        
        # Reset manual run flag
        setattr(current_app, 'manual_run', False)
        
        return jsonify({
            'success': True,
            'message': 'Missed processing recovery completed'
        })
            
    except Exception as e:
        logger.error(f"Error recovering missed processing: {str(e)}")
        # Reset manual run flag
        setattr(current_app, 'manual_run', False)
        return jsonify({
            'success': False,
            'message': str(e)
        }), 500


@tasks_bp.route('/api/tasks/<int:id>/upload', methods=['POST'])
def upload_task(id):
    """Manual upload of all pending videos for a task"""
    try:
        with db.get_connection() as conn:
            c = conn.cursor()
            c.execute("""
                SELECT id, status FROM tasks WHERE id = %s
            """, (id,))
            task = c.fetchone()
            
            if not task:
                return jsonify({
                    'success': False,
                    'message': f'Task {id} not found'
                }), 404
            
            if task[1] == 'running':
                return jsonify({
                    'success': False,
                    'message': f'Task {id} is currently running'
                }), 409

            # Get all pending videos for this task
            c.execute("""
                SELECT id, original_name, processed_path, scheduled_time
                FROM generated_videos
                WHERE task_id = %s 
                AND upload_status = 'pending'
                AND processed_path IS NOT NULL
                ORDER BY scheduled_time ASC
            """, (id,))
            pending_videos = c.fetchall()
            
            if not pending_videos:
                return jsonify({
                    'success': False,
                    'message': f'No pending videos found for task {id}'
                }), 404

            success_count = 0
            for video in pending_videos:
                if not os.path.exists(video[2]):
                    continue

                try:
                    from webapp.core_app.core.pipeline import process_video_upload
                    if process_video_upload(id, (video[0], video[1], video[2], video[3])):
                        success_count += 1
                except Exception as e:
                    logger.error(f"Error uploading video {video[0]}: {str(e)}")
                    # Continue with next video

            if success_count > 0:
                return jsonify({
                    'success': True,
                    'message': f'Successfully uploaded {success_count} of {len(pending_videos)} videos'
                })
            else:
                return jsonify({
                    'success': False,
                    'message': 'Failed to upload any videos'
                }), 500
            
    except Exception as e:
        logger.error(f"Error uploading videos for task {id}: {str(e)}")
        return jsonify({
            'success': False,
            'message': str(e)
        }), 500
@tasks_bp.route('/api/tasks/<int:id>/status', methods=['GET'])
def get_task_status(id):
    """Get real-time status of a specific task with automatic cleanup"""
    try:
        with db.get_connection() as conn:
            c = conn.cursor()
            
            # First check if task is stuck and reset if needed
            c.execute("""
                UPDATE tasks 
                SET status = 'pending', processing_status = 'pending'
                WHERE id = %s AND status = 'previewing' 
                AND NOT EXISTS (
                    SELECT 1 FROM task_lock 
                    WHERE locked = 1 AND task_id = %s
                )
            """, (id, id))
            
            reset_count = c.rowcount
            if reset_count > 0:
                logger.info(f"Auto-reset stuck preview state for task {id}")
            
            # Get current status
            c.execute("""
                SELECT t.id, t.name, t.status, t.processing_status, 
                       tl.locked, tl.task_id as lock_task_id
                FROM tasks t
                LEFT JOIN task_lock tl ON tl.id = 1
                WHERE t.id = %s
            """, (id,))
            
            result = c.fetchone()
            if not result:
                return jsonify({'error': 'Task not found'}), 404
            
            task_id, name, status, processing_status, locked, lock_task_id = result
            
            # Determine actual status
            actual_status = status
            if locked and lock_task_id == task_id:
                actual_status = 'running'
            elif status == 'previewing' and (not locked or lock_task_id != task_id):
                # Task claims to be previewing but no lock - it's stuck
                actual_status = 'pending'
            
            conn.commit()
            
            return jsonify({
                'id': task_id,
                'name': name,
                'status': actual_status,
                'processing_status': processing_status,
                'locked': bool(locked),
                'lock_task_id': lock_task_id,
                'was_reset': reset_count > 0
            })
            
    except Exception as e:
        logger.error(f"Error getting task status for {id}: {str(e)}")
        return jsonify({'error': str(e)}), 500
@tasks_bp.route('/api/tasks/cleanup-stuck', methods=['POST'])
def cleanup_stuck_tasks_endpoint():
    """Manual endpoint to clean up any stuck tasks"""
    try:
        with db.get_connection() as conn:
            c = conn.cursor()
            
            # Reset any tasks stuck in 'previewing' status
            c.execute("""
                UPDATE tasks 
                SET status = 'pending', 
                    processing_status = 'pending'
                WHERE status = 'previewing'
            """)
            preview_reset_count = c.rowcount
            
            # Reset any tasks stuck in 'running' status (from unexpected shutdown)
            c.execute("""
                UPDATE tasks 
                SET status = 'pending', 
                    processing_status = 'pending'
                WHERE status = 'running'
            """)
            running_reset_count = c.rowcount
            
            # Force release any stuck locks
            c.execute("""
                UPDATE task_lock 
                SET locked = 0, 
                    task_id = NULL, 
                    locked_at = NULL 
                WHERE id = 1
            """)
            lock_reset_count = c.rowcount
            
            conn.commit()
            
            # Clean up orphaned preview files
            from webapp.core_app.core.pipeline import get_preview_dir
            import glob
            preview_dir = get_preview_dir()
            preview_files = glob.glob(os.path.join(preview_dir, 'preview_task_*.mp4'))
            cleaned_files = 0
            
            for preview_file in preview_files:
                try:
                    # Extract task ID from filename
                    filename = os.path.basename(preview_file)
                    if filename.startswith('preview_task_') and filename.endswith('.mp4'):
                        task_id_str = filename[13:-4]  # Remove 'preview_task_' and '.mp4'
                        try:
                            task_id = int(task_id_str)
                            # Check if task exists and is not in previewing state
                            c.execute("SELECT status FROM tasks WHERE id = %s", (task_id,))
                            result = c.fetchone()
                            if not result or result[0] != 'previewing':
                                # Safe to remove orphaned preview file
                                os.remove(preview_file)
                                cleaned_files += 1
                        except (ValueError, TypeError):
                            # Invalid task ID in filename, remove it
                            os.remove(preview_file)
                            cleaned_files += 1
                except Exception as file_error:
                    logger.warning(f"Failed to clean up preview file {preview_file}: {str(file_error)}")
            
            return jsonify({
                'success': True,
                'message': 'Cleanup completed',
                'details': {
                    'preview_tasks_reset': preview_reset_count,
                    'running_tasks_reset': running_reset_count,
                    'locks_released': lock_reset_count,
                    'preview_files_cleaned': cleaned_files
                }
            })
            
    except Exception as e:
        logger.error(f"Error during manual cleanup: {str(e)}")
        return jsonify({
            'success': False,
            'message': f'Cleanup failed: {str(e)}'
        }), 500

def auto_correct_task_statuses():
    """Automatically correct task statuses - called by scheduler"""
    try:
        from webapp.core_app.core.database import db
        
        with db.get_connection() as conn:
            c = conn.cursor()
            
            # Reset tasks that are stuck in previewing/running without locks
            c.execute("""
                UPDATE tasks 
                SET status = 'pending', processing_status = 'pending'
                WHERE status IN ('previewing', 'running') 
                AND NOT EXISTS (
                    SELECT 1 FROM task_lock 
                    WHERE locked = 1 AND task_id = tasks.id
                )
            """)
            
            reset_count = c.rowcount
            if reset_count > 0:
                logger.info(f"Auto-corrected {reset_count} stuck task statuses")
            
            conn.commit()
            
    except Exception as e:
        logger.error(f"Error in automatic status correction: {str(e)}")

@tasks_bp.route('/api/tasks/night-status', methods=['GET'])
def get_night_processing_status():
    """Get the status of night processing window and upcoming tasks"""
    try:
        from webapp.core_app.core.pipeline import should_process_at_night
        in_night_window = should_process_at_night()
        
        # Get configured times
        night_start = os.getenv('NIGHT_PROCESSING_START', '22:00')
        night_end = os.getenv('NIGHT_PROCESSING_END', '06:00')
        
        # Get upcoming tasks
        with db.get_connection() as conn:
            c = conn.cursor()
            
            tomorrow = datetime.now() + timedelta(days=1)
            tomorrow_day = tomorrow.strftime('%A')[:3].lower()
            
            c.execute("""
                SELECT id, name, schedule 
                FROM tasks 
                WHERE status != 'failed'
                AND processing_status != 'failed'
                AND schedule LIKE %s
            """, (f'%{tomorrow_day}|%',))
            
            tasks = c.fetchall()
            
            scheduled_tasks = []
            for task_id, task_name, schedule in tasks:
                # Parse schedule for this day
                day_schedules = schedule.split(';')
                for day_schedule in day_schedules:
                    if day_schedule.startswith(f"{tomorrow_day}|"):
                        day, times = day_schedule.split('|')
                        time_slots = [time.strip() for time in times.split(',')]
                        
                        scheduled_tasks.append({
                            'id': task_id,
                            'name': task_name,
                            'day': tomorrow_day,
                            'time_slots': time_slots
                        })
            
        return jsonify({
            'in_night_window': in_night_window,
            'night_start_time': night_start,
            'night_end_time': night_end,
            'current_time': datetime.now().strftime('%H:%M'),
            'scheduled_tasks': scheduled_tasks,
            'tomorrow_day': tomorrow.strftime('%A'),
            'tomorrow_date': tomorrow.strftime('%Y-%m-%d')
        })
        
    except Exception as e:
        logger.error(f"Error getting night processing status: {str(e)}")
        return jsonify({
            'success': False,
            'message': str(e)
        }), 500