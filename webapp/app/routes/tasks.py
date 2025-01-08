from flask import jsonify, request, Blueprint, current_app, send_file
import sqlite3
import json
from datetime import datetime, timedelta
import logging
import os
from app.timezone import localize_timestamp

# Create blueprint and logger
tasks_bp = Blueprint('tasks', __name__)
logger = logging.getLogger(__name__)

@tasks_bp.route('/api/tasks', methods=['GET'])
def get_tasks():
    with sqlite3.connect('pipeline.db') as conn:
        c = conn.cursor()
        c.execute("""
            SELECT t.*, g.name as generator_name 
            FROM tasks t
            JOIN generators g ON t.generator_id = g.id
            ORDER BY t.created_at DESC
        """)
        tasks = c.fetchall()
        
        # Get utilities for each task
        task_list = []
        for t in tasks:
            utilities = []
            if t[3]:  # utilities JSON string
                util_ids = json.loads(t[3])
                if util_ids:  # Check if there are any utilities
                    c.execute("SELECT id, name FROM utilities WHERE id IN (%s)" % 
                             ','.join('?' * len(util_ids)), util_ids)
                    utilities = c.fetchall()

            task_list.append({
                'id': t[0],
                'name': t[1],
                'generator_id': t[2],
                'generator_name': t[12],
                'utilities': [{'id': u[0], 'name': u[1]} for u in utilities],
                'schedule': t[4],
                'platforms': json.loads(t[5] if t[5] else '[]'),
                'hashtags': t[6],
                'sound_name': t[7],
                'sound_volume': t[8],
                'status': t[9],
                'email_notify': t[10],
                'created_at': localize_timestamp(t[11])  # Convert timestamp to local time
            })
        
        return jsonify(task_list)

def retry_with_backoff(task_id):
    """Retry task with exponential backoff"""
    scheduler = current_app.scheduler
    
    # Get current retry count
    with sqlite3.connect('pipeline.db') as conn:
        c = conn.cursor()
        c.execute("SELECT retry_count FROM tasks WHERE id = ?", (task_id,))
        result = c.fetchone()
        retry_count = result[0] if result and result[0] is not None else 0
        
        if retry_count >= 3:  # Max retries
            c.execute("""
                UPDATE tasks 
                SET status = 'failed', 
                    retry_count = 0
                WHERE id = ?
            """, (task_id,))
            conn.commit()
            return
        
        # Calculate delay using exponential backoff (1min, 2min, 4min)
        delay = 60 * (2 ** retry_count)
        
        # Schedule retry
        next_run = datetime.now() + timedelta(seconds=delay)
        from app.core.pipeline import process_video_pipeline
        scheduler.add_job(
            func=process_video_pipeline,
            trigger='date',
            run_date=next_run,
            args=[task_id],
            id=f'retry_task_{task_id}_{next_run.timestamp()}'
        )
        
        # Update retry count
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
    
    with sqlite3.connect('pipeline.db') as conn:
        c = conn.cursor()
        
        # Check if retry_count column exists
        try:
            c.execute("SELECT retry_count FROM tasks LIMIT 1")
        except sqlite3.OperationalError:
            try:
                c.execute("ALTER TABLE tasks ADD COLUMN retry_count INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                # Column might have been added by another concurrent request
                pass
        
        # Convert lists to JSON strings
        utilities_json = json.dumps(data.get('utilities', []))
        platforms_json = json.dumps(data['platforms'])
        
        c.execute('''INSERT INTO tasks 
                    (name, generator_id, utilities, schedule, platforms, 
                     hashtags, sound_name, sound_volume, email_notify, retry_count)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)''',
                 (data['name'], data['generator_id'], utilities_json,
                  data['schedule'], platforms_json, data.get('hashtags'),
                  data.get('sound_name'), data.get('sound_volume', 'background'),
                  data.get('email_notify')))
        task_id = c.lastrowid
        
        # Schedule multiple times if needed
        schedule_times = data['schedule'].split(',')
        from app.core.pipeline import process_video_pipeline
        for schedule_time in schedule_times:
            hour, minute = map(int, schedule_time.strip().split(':'))
            job_id = f'task_{task_id}_{hour}_{minute}'
            scheduler.add_job(
                func=process_video_pipeline,
                trigger='cron',
                hour=hour,
                minute=minute,
                args=[task_id],
                id=job_id,
                misfire_grace_time=45  # Allow 45 seconds grace time for misfires
            )
        
        return jsonify({'id': task_id, 'status': 'scheduled'})

@tasks_bp.route('/api/tasks/<int:id>', methods=['DELETE'])
def delete_task(id):
    scheduler = current_app.scheduler
    
    with sqlite3.connect('pipeline.db') as conn:
        c = conn.cursor()
        # Remove any scheduled jobs first
        c.execute('SELECT schedule FROM tasks WHERE id = ?', (id,))
        task = c.fetchone()
        if task and task[0]:
            # Remove both regular and retry jobs
            schedule_times = task[0].split(',')
            for time in schedule_times:
                try:
                    scheduler.remove_job(f'task_{id}_{time.strip().replace(":", "_")}')
                except:
                    pass
            # Remove any pending retry jobs
            for job in scheduler.get_jobs():
                if job.id.startswith(f'retry_task_{id}'):
                    try:
                        scheduler.remove_job(job.id)
                    except:
                        pass
                        
        c.execute('DELETE FROM tasks WHERE id = ?', (id,))
        return jsonify({'success': True})

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
    video_path = None
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

        # Run the pipeline in preview mode
        from app.core.pipeline import process_video_pipeline
        video_path = process_video_pipeline(id, preview_mode=True)
        
        if video_path and os.path.exists(video_path):
            try:
                # Return the video file
                return send_file(
                    video_path,
                    mimetype='video/mp4',
                    as_attachment=True,
                    download_name=f'preview_task_{id}.mp4'
                )
            finally:
                # Clean up after sending
                try:
                    if os.path.exists(video_path):
                        os.remove(video_path)
                except Exception as e:
                    logger.error(f"Error cleaning up preview file {video_path}: {e}")
        else:
            return jsonify({
                'success': False,
                'message': 'Failed to generate preview video'
            }), 500
            
    except Exception as e:
        logger.error(f"Error previewing task {id}: {str(e)}")
        # Clean up if there was an error
        if video_path and os.path.exists(video_path):
            try:
                os.remove(video_path)
            except Exception as cleanup_error:
                logger.error(f"Error cleaning up preview file after error: {cleanup_error}")
        return jsonify({
            'success': False,
            'message': str(e)
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