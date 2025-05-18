from flask import jsonify, request, Blueprint
import logging
from webapp.core_app.timezone import localize_timestamp
from webapp.core_app.core.database import db

# Create blueprint and logger
platforms_bp = Blueprint('platforms', __name__)
logger = logging.getLogger(__name__)

@platforms_bp.route('/api/platforms', methods=['GET'])
def get_platforms():
    with db.get_connection() as conn:
        with conn.cursor() as c:
            c.execute("SELECT * FROM platforms")
            platforms = c.fetchall()
            return jsonify([{
                'id': p[0],
                'name': p[1],
                'uploader_curl': p[2],
                'fallback_curl': p[3],
                'fallback_curl_2': p[4],
                'default_hashtags': p[5],
                'created_at': localize_timestamp(p[6])
            } for p in platforms])

@platforms_bp.route('/api/platforms', methods=['POST'])
def create_platform():
    data = request.json
    with db.get_connection() as conn:
        with conn.cursor() as c:
            c.execute('''INSERT INTO platforms 
                        (name, uploader_curl, fallback_curl, fallback_curl_2, default_hashtags)
                        VALUES (%s, %s, %s, %s, %s) RETURNING id''',
                    (data['name'], data['uploader_curl'],
                    data.get('fallback_curl'), data.get('fallback_curl_2'), 
                    data.get('default_hashtags')))
            platform_id = c.fetchone()[0]
        conn.commit()
        return jsonify({'id': platform_id})

@platforms_bp.route('/api/platforms/<int:id>', methods=['DELETE'])
def delete_platform(id):
    with db.get_connection() as conn:
        with conn.cursor() as c:
            # First delete any task platform account associations
            c.execute('DELETE FROM task_platform_accounts WHERE platform_id = %s', (id,))
            # Then delete the platform
            c.execute('DELETE FROM platforms WHERE id = %s', (id,))
        conn.commit()
        return jsonify({'success': True})

@platforms_bp.route('/api/tasks/<int:task_id>/platforms', methods=['GET'])
def get_task_platforms(task_id):
    with db.get_connection() as conn:
        with conn.cursor() as c:
            c.execute('''
                SELECT p.*, tpa.account_name 
                FROM platforms p
                JOIN task_platform_accounts tpa ON p.id = tpa.platform_id
                WHERE tpa.task_id = %s
            ''', (task_id,))
            platforms = c.fetchall()
            return jsonify([{
                'id': p[0],
                'name': p[1],
                'uploader_curl': p[2],
                'fallback_curl': p[3],
                'fallback_curl_2': p[4],
                'default_hashtags': p[5],
                'created_at': localize_timestamp(p[6]),
                'account_name': p[7]
            } for p in platforms])

@platforms_bp.route('/api/tasks/<int:task_id>/platforms', methods=['POST'])
def add_task_platform(task_id):
    data = request.json
    with db.get_connection() as conn:
        with conn.cursor() as c:
            c.execute('''INSERT INTO task_platform_accounts 
                        (task_id, platform_id, account_name)
                        VALUES (%s, %s, %s)''',
                    (task_id, data['platform_id'], data['account_name']))
        conn.commit()
        return jsonify({'success': True})

@platforms_bp.route('/api/tasks/<int:task_id>/platforms/<int:platform_id>', methods=['DELETE'])
def remove_task_platform(task_id, platform_id):
    with db.get_connection() as conn:
        with conn.cursor() as c:
            c.execute('''DELETE FROM task_platform_accounts 
                        WHERE task_id = %s AND platform_id = %s''',
                    (task_id, platform_id))
        conn.commit()
        return jsonify({'success': True})