from flask import Flask, render_template, request, jsonify
import sqlite3
import logging
from log_manager import get_logs, clear_old_logs, init_logs

# Flask app initialization
app = Flask(__name__)

# Configure logger
logger = logging.getLogger('app_logger')
logger.setLevel(logging.INFO)
ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
ch.setFormatter(formatter)
logger.addHandler(ch)

# Initialize logs table
init_logs()

# Basic routes
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/manage')
def manage():
    return render_template('manage.html')

@app.route('/logs')
def logs():
    return render_template('logs.html')

# Log routes
@app.route('/api/logs', methods=['GET'])
def get_log_entries():
    """Get filtered log entries"""
    try:
        # Parse query parameters with defaults
        limit = min(int(request.args.get('limit', 100)), 1000)  # Cap at 1000 entries
        level = request.args.get('level')
        task_id = request.args.get('task_id')
        since = request.args.get('since')
        
        # Get logs with filters
        logs = get_logs(
            limit=limit,
            level=level,
            task_id=task_id,
            since=since
        )
        
        return jsonify({
            'logs': logs,
            'count': len(logs)
        })
        
    except Exception as e:
        return jsonify({
            'error': str(e)
        }), 500

# Generator routes
@app.route('/api/generators', methods=['GET'])
def get_generators():
    with sqlite3.connect('pipeline.db') as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM generators")
        generators = c.fetchall()
        return jsonify([{
            'id': g[0],
            'name': g[1],
            'generator_curl': g[2],
            'created_at': g[3]
        } for g in generators])

@app.route('/api/generators', methods=['POST'])
def create_generator():
    data = request.json
    with sqlite3.connect('pipeline.db') as conn:
        c = conn.cursor()
        c.execute('INSERT INTO generators (name, generator_curl) VALUES (?, ?)',
                 (data['name'], data['generator_curl']))
        generator_id = c.lastrowid
        return jsonify({'id': generator_id})

@app.route('/api/generators/<int:id>', methods=['DELETE'])
def delete_generator(id):
    with sqlite3.connect('pipeline.db') as conn:
        c = conn.cursor()
        c.execute('DELETE FROM generators WHERE id = ?', (id,))
        return jsonify({'success': True})

# Utility routes
@app.route('/api/utilities', methods=['GET'])
def get_utilities():
    with sqlite3.connect('pipeline.db') as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM utilities")
        utilities = c.fetchall()
        return jsonify([{
            'id': u[0],
            'name': u[1],
            'utility_curl': u[2],
            'created_at': u[3]
        } for u in utilities])

@app.route('/api/utilities', methods=['POST'])
def create_utility():
    data = request.json
    with sqlite3.connect('pipeline.db') as conn:
        c = conn.cursor()
        c.execute('INSERT INTO utilities (name, utility_curl) VALUES (?, ?)',
                 (data['name'], data['utility_curl']))
        utility_id = c.lastrowid
        return jsonify({'id': utility_id})

@app.route('/api/utilities/<int:id>', methods=['DELETE'])
def delete_utility(id):
    with sqlite3.connect('pipeline.db') as conn:
        c = conn.cursor()
        c.execute('DELETE FROM utilities WHERE id = ?', (id,))
        return jsonify({'success': True})

# Platform routes
@app.route('/api/platforms', methods=['GET'])
def get_platforms():
    with sqlite3.connect('pipeline.db') as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM platform_accounts")
        platforms = c.fetchall()
        return jsonify([{
            'id': p[0],
            'platform': p[1],
            'account_name': p[2],
            'uploader_curl': p[3],
            'fallback_curl': p[4],
            'fallback_curl_2': p[5],
            'default_hashtags': p[6],
            'created_at': p[7]
        } for p in platforms])

@app.route('/api/platforms', methods=['POST'])
def create_platform():
    data = request.json
    with sqlite3.connect('pipeline.db') as conn:
        c = conn.cursor()
        c.execute('''INSERT INTO platform_accounts 
                    (platform, account_name, uploader_curl, fallback_curl, fallback_curl_2, default_hashtags)
                    VALUES (?, ?, ?, ?, ?, ?)''',
                 (data['platform'], data['account_name'], data['uploader_curl'],
                  data.get('fallback_curl'), data.get('fallback_curl_2'), data.get('default_hashtags')))
        platform_id = c.lastrowid
        return jsonify({'id': platform_id})

@app.route('/api/platforms/<int:id>', methods=['DELETE'])
def delete_platform(id):
    with sqlite3.connect('pipeline.db') as conn:
        c = conn.cursor()
        c.execute('DELETE FROM platform_accounts WHERE id = ?', (id,))
        return jsonify({'success': True})

# Task routes
@app.route('/api/tasks', methods=['GET'])
def get_tasks():
    with sqlite3.connect('pipeline.db') as conn:
        c = conn.cursor()
        c.execute("""
            SELECT 
                t.*,
                g.name as generator_name,
                (
                    SELECT GROUP_CONCAT(DISTINCT u.name)
                    FROM utilities u
                    WHERE u.id IN (
                        WITH RECURSIVE split(id, value, str) AS (
                            SELECT 1, NULL, t.utilities || ',' as str
                            UNION ALL
                            SELECT 
                                id+1, 
                                SUBSTR(str, 0, INSTR(str, ',')),
                                SUBSTR(str, INSTR(str, ',')+1)
                            FROM split WHERE str != ''
                        )
                        SELECT CAST(value AS INTEGER) FROM split WHERE value IS NOT NULL
                    )
                ) as utility_names,
                (
                    SELECT GROUP_CONCAT(DISTINCT pa.platform || ' - ' || pa.account_name)
                    FROM platform_accounts pa
                    WHERE pa.id IN (
                        WITH RECURSIVE split(id, value, str) AS (
                            SELECT 1, NULL, t.platforms || ',' as str
                            UNION ALL
                            SELECT 
                                id+1, 
                                SUBSTR(str, 0, INSTR(str, ',')),
                                SUBSTR(str, INSTR(str, ',')+1)
                            FROM split WHERE str != ''
                        )
                        SELECT CAST(value AS INTEGER) FROM split WHERE value IS NOT NULL
                    )
                ) as platform_names
            FROM tasks t
            LEFT JOIN generators g ON t.generator_id = g.id
        """)
        tasks = c.fetchall()
        
        # Convert to list of dicts with proper type conversion
        return jsonify([{
            'id': t[0],
            'name': t[1],
            'generator_id': t[2],
            'utilities': [int(x) for x in t[3].split(',')] if t[3] else [],
            'schedule': t[4],
            'platforms': [int(x) for x in t[5].split(',')] if t[5] else [],
            'hashtags': t[6],
            'sound_name': t[7],
            'sound_volume': t[8],
            'status': t[9],
            'email_notify': t[10],
            'retry_count': t[11],
            'created_at': t[12],
            'generator_name': t[13],
            'utility_names': t[14].split(',') if t[14] else [],
            'platform_names': t[15].split(',') if t[15] else []
        } for t in tasks])

@app.route('/api/tasks', methods=['POST'])
def create_task():
    data = request.json
    with sqlite3.connect('pipeline.db') as conn:
        c = conn.cursor()
        
        # Convert lists to comma-separated strings
        utilities = ','.join(map(str, data.get('utilities', [])))
        platforms = ','.join(map(str, data.get('platforms', [])))
        
        c.execute('''
            INSERT INTO tasks (
                name, generator_id, utilities, schedule, platforms,
                hashtags, sound_name, sound_volume, status, email_notify
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            data['name'],
            data['generator_id'],
            utilities,
            data['schedule'],
            platforms,
            data.get('hashtags'),
            data.get('sound_name'),
            data.get('sound_volume', 'background'),
            'pending',  # Default status
            data.get('email_notify')
        ))
        task_id = c.lastrowid
        
        # Return the created task with full details
        c.execute("""
            SELECT 
                t.*,
                g.name as generator_name,
                (
                    SELECT GROUP_CONCAT(DISTINCT u.name)
                    FROM utilities u
                    WHERE u.id IN (
                        WITH RECURSIVE split(id, value, str) AS (
                            SELECT 1, NULL, t.utilities || ',' as str
                            UNION ALL
                            SELECT 
                                id+1, 
                                SUBSTR(str, 0, INSTR(str, ',')),
                                SUBSTR(str, INSTR(str, ',')+1)
                            FROM split WHERE str != ''
                        )
                        SELECT CAST(value AS INTEGER) FROM split WHERE value IS NOT NULL
                    )
                ) as utility_names,
                (
                    SELECT GROUP_CONCAT(DISTINCT pa.platform || ' - ' || pa.account_name)
                    FROM platform_accounts pa
                    WHERE pa.id IN (
                        WITH RECURSIVE split(id, value, str) AS (
                            SELECT 1, NULL, t.platforms || ',' as str
                            UNION ALL
                            SELECT 
                                id+1, 
                                SUBSTR(str, 0, INSTR(str, ',')),
                                SUBSTR(str, INSTR(str, ',')+1)
                            FROM split WHERE str != ''
                        )
                        SELECT CAST(value AS INTEGER) FROM split WHERE value IS NOT NULL
                    )
                ) as platform_names
            FROM tasks t
            LEFT JOIN generators g ON t.generator_id = g.id
            WHERE t.id = ?
        """, (task_id,))
        task = c.fetchone()
        
        return jsonify({
            'id': task[0],
            'name': task[1],
            'generator_id': task[2],
            'utilities': [int(x) for x in task[3].split(',')] if task[3] else [],
            'schedule': task[4],
            'platforms': [int(x) for x in task[5].split(',')] if task[5] else [],
            'hashtags': task[6],
            'sound_name': task[7],
            'sound_volume': task[8],
            'status': task[9],
            'email_notify': task[10],
            'retry_count': task[11],
            'created_at': task[12],
            'generator_name': task[13],
            'utility_names': task[14].split(',') if task[14] else [],
            'platform_names': task[15].split(',') if task[15] else []
        })

@app.route('/api/tasks/<int:id>', methods=['PUT'])
def update_task(id):
    data = request.json
    with sqlite3.connect('pipeline.db') as conn:
        c = conn.cursor()
        # Convert lists to comma-separated strings if present
        if 'utilities' in data:
            data['utilities'] = ','.join(map(str, data['utilities']))
        if 'platforms' in data:
            data['platforms'] = ','.join(map(str, data['platforms']))
        
        # Build update query dynamically based on provided fields
        fields = []
        values = []
        for key, value in data.items():
            if key in ['name', 'generator_id', 'utilities', 'schedule', 'platforms', 
                      'hashtags', 'sound_name', 'sound_volume', 'status', 'email_notify']:
                fields.append(f'{key} = ?')
                values.append(value)
        
        if fields:
            values.append(id)  # Add id for WHERE clause
            query = f'''UPDATE tasks SET {', '.join(fields)} WHERE id = ?'''
            c.execute(query, values)
            
        return jsonify({'success': True})

@app.route('/api/tasks/<int:id>', methods=['DELETE'])
def delete_task(id):
    with sqlite3.connect('pipeline.db') as conn:
        c = conn.cursor()
        c.execute('DELETE FROM tasks WHERE id = ?', (id,))
        return jsonify({'success': True})