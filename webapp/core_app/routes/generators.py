from flask import jsonify, request, Blueprint
from webapp.core_app.timezone import localize_timestamp
from webapp.core_app.core.database import db

# Create blueprint
generators_bp = Blueprint('generators', __name__)

@generators_bp.route('/api/generators', methods=['GET'])
def get_generators():
    with db.get_connection() as conn:
        with conn.cursor() as c:
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
    with db.get_connection() as conn:
        with conn.cursor() as c:
            c.execute('INSERT INTO generators (name, generator_curl) VALUES (%s, %s) RETURNING id',
                     (data['name'], data['generator_curl']))
            generator_id = c.fetchone()[0]
        conn.commit()
        return jsonify({'id': generator_id})

@generators_bp.route('/api/generators/<int:id>', methods=['DELETE'])
def delete_generator(id):
    with db.get_connection() as conn:
        with conn.cursor() as c:
            c.execute('DELETE FROM generators WHERE id = %s', (id,))
        conn.commit()
        return jsonify({'success': True})