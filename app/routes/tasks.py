from flask import jsonify, request, render_template
import sqlite3
import json
from datetime import datetime, timedelta
from app import app, scheduler
from app.core.pipeline import process_video_pipeline

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/manage')
def manage():
    return render_template('manage.html')

@app.route('/api/tasks', methods=['GET'])
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
                'created_at': t[11]
            })
        
        return jsonify(task_list)

def retry_with_backoff(task_id):
    """Retry task with exponential backoff"""
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
        scheduler.add_job(
            process_video_pipeline,
            'date',
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

@app.route('/api/tasks', methods=['POST'])
def create_task():
    data = request.json
    with sqlite3.connect('pipeline.db') as conn:
        c = conn.cursor()
        
        # Add retry_count column if it doesn't exist
        c.execute("""
            SELECT name FROM sqlite_master 
            WHERE type='table' AND name='tasks'
        """)
        if 'retry_count' not in [col[1] for col in c.description]:
            c.execute("ALTER TABLE tasks ADD COLUMN retry_count INTEGER DEFAULT 0")
        
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
        for schedule_time in schedule_times:
            hour, minute = map(int, schedule_time.strip().split(':'))
            scheduler.add_job(
                process_video_pipeline,
                'cron',
                hour=hour,
                minute=minute,
                args=[task_id],
                id=f'task_{task_id}_{hour}_{minute}',
                misfire_grace_time=45  # Allow 45 seconds grace time for misfires
            )
        
        return jsonify({'id': task_id, 'status': 'scheduled'})

@app.route('/api/tasks/<int:id>', methods=['DELETE'])
def delete_task(id):
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

@app.route('/api/tasks/<int:id>/run', methods=['POST'])
def run_task(id):
    try:
        process_video_pipeline(id)
        return jsonify({'success': True, 'message': 'Task started successfully'})
    except Exception as e:
        # If task fails due to lock, trigger retry mechanism
        retry_with_backoff(id)
        return jsonify({
            'success': False, 
            'message': 'Task is queued for retry due to lock',
            'error': str(e)
        }), 503