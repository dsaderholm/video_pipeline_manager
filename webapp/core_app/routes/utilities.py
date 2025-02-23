from flask import jsonify, request, Blueprint
import logging
import os
from webapp.core_app.timezone import localize_timestamp
from webapp.core_app.core.database import db

# Create blueprint and logger
utilities_bp = Blueprint('utilities', __name__)
logger = logging.getLogger(__name__)

@utilities_bp.route('/api/utilities', methods=['GET'])
def get_utilities():
    with db.get_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM utilities")
        utilities = c.fetchall()
        return jsonify([{
            'id': u[0],
            'name': u[1],
            'utility_curl': u[2],
            'created_at': localize_timestamp(u[3])
        } for u in utilities])

@utilities_bp.route('/api/utilities', methods=['POST'])
def create_utility():
    data = request.json
    with db.get_connection() as conn:
        c = conn.cursor()
        c.execute('INSERT INTO utilities (name, utility_curl) VALUES (?, ?)',
                 (data['name'], data['utility_curl']))
        utility_id = c.lastrowid
        conn.commit()
        return jsonify({'id': utility_id})

@utilities_bp.route('/api/utilities/<int:id>', methods=['DELETE'])
def delete_utility(id):
    with db.get_connection() as conn:
        c = conn.cursor()
        c.execute('DELETE FROM utilities WHERE id = ?', (id,))
        conn.commit()
        return jsonify({'success': True})