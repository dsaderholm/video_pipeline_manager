from flask import jsonify, request
import sqlite3
from app import app

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