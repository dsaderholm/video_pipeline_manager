from flask import jsonify, request
import sqlite3
from app import app

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
            'default_hashtags': p[5],
            'created_at': p[6]
        } for p in platforms])

@app.route('/api/platforms', methods=['POST'])
def create_platform():
    data = request.json
    with sqlite3.connect('pipeline.db') as conn:
        c = conn.cursor()
        c.execute('''INSERT INTO platform_accounts 
                    (platform, account_name, uploader_curl, fallback_curl, default_hashtags)
                    VALUES (?, ?, ?, ?, ?)''',
                 (data['platform'], data['account_name'], data['uploader_curl'],
                  data.get('fallback_curl'), data.get('default_hashtags')))
        platform_id = c.lastrowid
        return jsonify({'id': platform_id})

@app.route('/api/platforms/<int:id>', methods=['DELETE'])
def delete_platform(id):
    with sqlite3.connect('pipeline.db') as conn:
        c = conn.cursor()
        c.execute('DELETE FROM platform_accounts WHERE id = ?', (id,))
        return jsonify({'success': True})