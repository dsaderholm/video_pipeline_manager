from flask import jsonify, request
import sqlite3
from app import app

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