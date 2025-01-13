from flask import jsonify, request, Blueprint, current_app, send_from_directory, send_file
import sqlite3
import json
from datetime import datetime, timedelta
import logging
import os
import time
import glob
from app.timezone import localize_timestamp

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

def cleanup_old_previews(max_age_hours=24):
    """Clean up preview files older than the specified age"""
    try:
        from app.core.pipeline import get_preview_dir
        preview_dir = get_preview_dir()
        current_time = datetime.now()
        
        preview_files = glob.glob(os.path.join(preview_dir, 'preview_task_*.mp4'))
        
        for file_path in preview_files:
            try:
                mtime = datetime.fromtimestamp(os.path.getmtime(file_path))
                if current_time - mtime > timedelta(hours=max_age_hours):
                    try:
                        os.remove(file_path)
                        logger.info(f"Cleaned up old preview file: {file_path}")
                    except OSError as e:
                        logger.warning(f"Failed to delete old preview file {file_path}: {e}")
            except OSError as e:
                logger.warning(f"Error checking preview file {file_path}: {e}")
                
    except Exception as e:
        logger.error(f"Error during preview cleanup: {e}")

def should_cleanup():
    """Check if cleanup is needed (avoid cleaning up too frequently)"""
    try:
        from app.core.pipeline import get_preview_dir
        preview_dir = get_preview_dir()
        marker_file = os.path.join(preview_dir, '.last_cleanup')
        
        if not os.path.exists(marker_file):
            return True
            
        last_cleanup = datetime.fromtimestamp(os.path.getmtime(marker_file))
        return datetime.now() - last_cleanup > timedelta(hours=1)
        
    except Exception:
        return True

def cleanup_preview_dir():
    """Perform maintenance on the preview directory"""
    try:
        if not should_cleanup():
            logger.debug("Skipping cleanup - last cleanup was too recent")
            return

        from app.core.pipeline import get_preview_dir
        preview_dir = get_preview_dir()
        
        cleanup_old_previews()
        
        for root, dirs, files in os.walk(preview_dir, topdown=False):
            for name in dirs:
                try:
                    dir_path = os.path.join(root, name)
                    if not os.listdir(dir_path):
                        os.rmdir(dir_path)
                        logger.info(f"Removed empty preview subdirectory: {dir_path}")
                except OSError as e:
                    logger.warning(f"Failed to remove empty directory {name}: {e}")
        
        marker_file = os.path.join(preview_dir, '.last_cleanup')
        with open(marker_file, 'w') as f:
            f.write(datetime.now().isoformat())
                    
    except Exception as e:
        logger.error(f"Error during preview directory maintenance: {e}")

def get_task_details(task_id, conn):
    """Get detailed task information including platforms and utilities"""
    c = conn.cursor()
    
    # Get basic task info with generator name
    c.execute("""
        SELECT t.*, g.name as generator_name
        FROM tasks t
        LEFT JOIN generators g ON t.generator_id = g.id
        WHERE t.id = ?
    """, (task_id,))
    task = c.fetchone()
    
    if not task:
        return None
        
    # Get utilities
    utilities = []
    if task[3]:  # utilities column
        utility_ids = json.loads(task[3])
        if utility_ids:
            placeholders = ','.join('?' * len(utility_ids))
            c.execute(f"SELECT id, name FROM utilities WHERE id IN ({placeholders})", utility_ids)
            utilities = [{'id': u[0], 'name': u[1]} for u in c.fetchall()]

    # Get platforms with account names
    c.execute("""
        SELECT p.id, p.name, p.uploader_curl, p.fallback_curl, p.fallback_curl_2,
               p.default_hashtags, tpa.account_name
        FROM platforms p
        JOIN task_platform_accounts tpa ON p.id = tpa.platform_id
        WHERE tpa.task_id = ?
    """, (task_id,))
    platforms = c.fetchall()

    return {
        'id': task[0],
        'name': task[1],
        'generator_id': task[2],
        'generator_name': task[12],
        'utilities': utilities,
        'schedule': task[4],
        'hashtags': task[5],
        'sound_name': task[6],
        'sound_volume': task[7],
        'status': task[8],
        'email_notify': task[9],
        'retry_count': task[10],
        'created_at': localize_timestamp(task[11]),
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
    with sqlite3.connect('pipeline.db') as conn:
        tasks = []
        c = conn.cursor()
        
        c.execute("""
            SELECT t.*, g.name as generator_name
            FROM tasks t
            LEFT JOIN generators g ON t.generator_id = g.id
            ORDER BY t.created_at DESC
        """)
        task_rows = c.fetchall()
        
        for task in task_rows:
            task_detail = get_task_details(task[0], conn)
            if task_detail:
                tasks.append(task_detail)
                
        return jsonify(tasks)

def retry_with_backoff(task_id):
    """Retry task with exponential backoff"""
    scheduler = current_app.scheduler
    
    with sqlite3.connect('pipeline.db') as conn:
        c = conn.cursor()
        c.execute("SELECT retry_count FROM tasks WHERE id = ?", (task_id,))
        result = c.fetchone()
        retry_count = result[0] if result and result[0] is not None else 0
        
        if retry_count >= 3:
            c.execute("""
                UPDATE tasks 
                SET status = 'failed', 
                    retry_count = 0
                WHERE id = ?
            """, (task_id,))
            conn.commit()
            return
        
        delay = 60 * (2 ** retry_count)
        next_run = datetime.now() + timedelta(seconds=delay)
        
        from app.core.pipeline import process_video_pipeline
        scheduler.add_job(
            func=process_video_pipeline,
            trigger='date',
            run_date=next_run,
            args=[task_id],
            id=f'retry_task_{task_id}_{next_run.timestamp()}'
        )
        
        c.execute("""
            UPDATE tasks 
            SET retry_count = ?,
                status = 'retrying'
            WHERE id = ?
        """, (retry_count + 1, task_id))
        conn.commit()

@tasks_bp.route('/api/tasks', methods=['POST'])
def create_task():
    data = request.json
    scheduler = current_app.scheduler
    
    # Validate schedule format
    is_valid, error_message = validate_schedule(data['schedule'])
    if not is_valid:
        return jsonify({'success': False, 'message': error_message}), 400
    
    with sqlite3.connect('pipeline.db') as conn:
        try:
            conn.execute('BEGIN')
            c = conn.cursor()
            
            # Insert the main task
            utilities_json = json.dumps(data.get('utilities', []))
            c.execute('''INSERT INTO tasks 
                        (name, generator_id, utilities, schedule, 
                         hashtags, sound_name, sound_volume, email_notify, retry_count)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)''',
                     (data['name'], data['generator_id'], utilities_json,
                      data['schedule'], data.get('hashtags'),
                      data.get('sound_name'), data.get('sound_volume', 'background'),
                      data.get('email_notify')))
            task_id = c.lastrowid
            
            # Insert platform associations
            for platform in data.get('platforms', []):
                c.execute('''INSERT INTO task_platform_accounts 
                            (task_id, platform_id, account_name)
                            VALUES (?, ?, ?)''',
                         (task_id, platform['id'], platform['account_name']))
            
            # Schedule the task
            day_schedules = data['schedule'].split(';')
            from app.core.pipeline import process_video_pipeline
            
            for day_schedule in day_schedules:
                if not day_schedule.strip():
                    continue
                    
                day, times = day_schedule.split('|')
                day_number = {
                    'sun': 0, 'mon': 1, 'tue': 2, 'wed': 3, 
                    'thu': 4, 'fri': 5, 'sat': 6
                }[day.lower()]
                
                for time in times.split(','):
                    hour, minute = map(int, time.strip().split(':'))
                    job_id = f'task_{task_id}_{day}_{hour}_{minute}'
                    
                    scheduler.add_job(
                        func=process_video_pipeline,
                        trigger='cron',
                        day_of_week=day_number,
                        hour=hour,
                        minute=minute,
                        args=[task_id],
                        id=job_id,
                        misfire_grace_time=45
                    )
                    logger.info(f"Scheduled task {task_id} for {day} at {hour:02d}:{minute:02d}")
            
            conn.commit()
            return jsonify({'id': task_id, 'status': 'scheduled'})
            
        except Exception as e:
            conn.rollback()
            logger.error(f"Error creating task: {str(e)}")
            return jsonify({'success': False, 'message': str(e)}), 500

@tasks_bp.route('/api/tasks/<int:id>', methods=['DELETE'])
def delete_task(id):
    scheduler = current_app.scheduler
    
    with sqlite3.connect('pipeline.db') as conn:
        try:
            conn.execute('BEGIN')
            c = conn.cursor()
            
            # Remove scheduled jobs
            c.execute('SELECT schedule FROM tasks WHERE id = ?', (id,))
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

                # Remove retry jobs
                for job in scheduler.get_jobs():
                    if job.id.startswith(f'retry_task_{id}'):
                        try:
                            scheduler.remove_job(job.id)
                        except:
                            pass
            
            # Delete task platform associations (will cascade)
            c.execute('DELETE FROM task_platform_accounts WHERE task_id = ?', (id,))
            
            # Delete the task
            c.execute('DELETE FROM tasks WHERE id = ?', (id,))
            
            conn.commit()
            return jsonify({'success': True})
            
        except Exception as e:
            conn.rollback()
            logger.error(f"Error deleting task: {str(e)}")
            return jsonify({'success': False, 'message': str(e)}), 500

@tasks_bp.route('/api/tasks/<int:id>/run', methods=['POST'])
def run_task(id):
    try:
        # First verify task exists and is not already running
        with sqlite3.connect('pipeline.db') as conn:
            c = conn.cursor()
            c.execute("SELECT id, status FROM tasks WHERE id = ?", (id,))
            task = c.fetchone()
            if not task:
                return jsonify({
                    'success': False,
                    'message': f'Task {id} not found'
                }), 404
            if task[1] == 'running':
                return jsonify({
                    'success': False,
                    'message': f'Task {id} is already running'
                }), 409

            # Clear any stuck locks before running
            c.execute("""
                UPDATE task_lock 
                SET locked = 0, 
                    task_id = NULL, 
                    locked_at = NULL 
                WHERE id = 1 
                AND datetime(locked_at, '+30 minutes') < datetime('now')
            """)
            conn.commit()
                
        from app.core.pipeline import process_video_pipeline
        process_video_pipeline(id)
        return jsonify({
            'success': True,
            'message': 'Task started successfully'
        })
    except Exception as e:
        logger.error(f"Error running task {id}: {str(e)}")
        # If task fails due to lock, trigger retry mechanism
        retry_with_backoff(id)
        return jsonify({
            'success': False,
            'message': 'Task is queued for retry',
            'error': str(e)
        }), 503

@tasks_bp.route('/api/tasks/<int:id>/preview', methods=['POST'])
def preview_task(id):
    try:
        # Run cleanup before generating new preview
        cleanup_preview_dir()
        
        with sqlite3.connect('pipeline.db') as conn:
            conn.isolation_level = None  # Enable autocommit mode
            c = conn.cursor()
            
            # Begin transaction
            c.execute("BEGIN IMMEDIATE")
            
            # Now check this specific task - within transaction
            c.execute("SELECT id, status FROM tasks WHERE id = ?", (id,))
            task = c.fetchone()
            
            if not task:
                c.execute("COMMIT")
                return jsonify({
                    'success': False,
                    'message': f'Task {id} not found'
                }), 404
                
            if task[1] == 'previewing':
                c.execute("COMMIT")
                return jsonify({
                    'success': False,
                    'message': 'Preview already in progress'
                }), 409

            # Clean up any existing preview files for this task
            from app.core.pipeline import get_preview_dir
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

            # Update task status for preview within transaction
            c.execute("""
                UPDATE tasks 
                SET status = 'previewing'
                WHERE id = ?
            """, (id,))
            
            # Commit transaction
            c.execute("COMMIT")

        # Start the pipeline in preview mode
        from app.core.pipeline import process_video_pipeline
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
                with sqlite3.connect('pipeline.db') as conn:
                    c = conn.cursor()
                    c.execute("""
                        UPDATE tasks 
                        SET status = 'completed'
                        WHERE id = ?
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
        # Update task status on error
        with sqlite3.connect('pipeline.db') as conn:
            c = conn.cursor()
            c.execute("""
                UPDATE tasks 
                SET status = 'failed'
                WHERE id = ?
            """, (id,))
            conn.commit()
            
        return jsonify({
            'success': False,
            'message': f'Preview generation failed: {str(e)}'
        }), 500

@tasks_bp.route('/api/tasks/<int:id>/preview/download', methods=['GET'])
def download_preview(id):
    try:
        from app.core.pipeline import get_preview_dir
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

@tasks_bp.route('/api/tasks/<int:id>/dry-run', methods=['POST'])
def dry_run_task(id):
    try:
        # First verify task exists
        with sqlite3.connect('pipeline.db') as conn:
            c = conn.cursor()
            c.execute("SELECT id, status FROM tasks WHERE id = ?", (id,))
            task = c.fetchone()
            if not task:
                return jsonify({
                    'success': False,
                    'message': f'Task {id} not found'
                }), 404

        # Run the pipeline in dry run mode
        from app.core.pipeline import process_video_pipeline
        success = process_video_pipeline(id, dry_run=True)
        
        return jsonify({
            'success': success,
            'message': 'Dry run completed successfully' if success else 'Dry run failed'
        })
            
    except Exception as e:
        logger.error(f"Error dry running task {id}: {str(e)}")
        return jsonify({
            'success': False,
            'message': str(e)
        }), 500