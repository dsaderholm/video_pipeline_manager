from flask import jsonify, request, Blueprint
import sqlite3
import os
from flask import current_app
from app.timezone import localize_timestamp

def get_db_path():
    return os.path.join('webapp', 'database', 'pipeline.db')

# Create blueprint
generators_bp = Blueprint('generators', __name__)

@generators_bp.route('/api/generators', methods=['GET'])
def get_generators():
    with sqlite3.connect(get_db_path()) as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM generators")
        generators = c.fetchall()
        return jsonify([{
            'id': g[0],
            'name': g[1],
            'generator_curl': g[2],
            'created_at': localize_timestamp(g[3])
        } for g in generators])

@generators_bp.route('/api/generators', methods=['POST'])
def create_generator():
    data = request.json
    with sqlite3.connect(get_db_path()) as conn:
        c = conn.cursor()
        c.execute('INSERT INTO generators (name, generator_curl) VALUES (?, ?)',
                 (data['name'], data['generator_curl']))
        generator_id = c.lastrowid
        return jsonify({'id': generator_id})

@generators_bp.route('/api/generators/<int:id>', methods=['DELETE'])
def delete_generator(id):
    with sqlite3.connect(get_db_path()) as conn:
        c = conn.cursor()
        c.execute('DELETE FROM generators WHERE id = ?', (id,))
        return jsonify({'success': True})