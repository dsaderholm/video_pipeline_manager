from flask import jsonify, request, Blueprint
import sqlite3
import logging
from app.timezone import localize_timestamp

# Create blueprint and logger
platforms_bp = Blueprint('platforms', __name__)
logger = logging.getLogger(__name__)

@platforms_bp.route('/api/platforms', methods=['GET'])
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
            'created_at': localize_timestamp(p[7])
        } for p in platforms])

@platforms_bp.route('/api/platforms', methods=['POST'])
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

@platforms_bp.route('/api/platforms/<int:id>', methods=['DELETE'])
def delete_platform(id):
    with sqlite3.connect('pipeline.db') as conn:
        c = conn.cursor()
        c.execute('DELETE FROM platform_accounts WHERE id = ?', (id,))
        return jsonify({'success': True})