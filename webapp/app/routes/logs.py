from flask import Blueprint, jsonify, request, render_template
import json
from datetime import datetime, timedelta
from app.core.log_manager import get_logs, clear_old_logs
from app.timezone import localize_timestamp, get_timezone
import pytz

def get_db_path():
    return os.path.join('webapp', 'database', 'pipeline.db')

# Create blueprint
logs_bp = Blueprint('logs', __name__, url_prefix='')

@logs_bp.route('/logs')
def logs_page():
    """Render the logs viewer page"""
    return render_template('logs.html', 
                         title='Logs Viewer',
                         active_page='logs')

@logs_bp.route('/api/logs', methods=['GET'])
def get_log_entries():
    """Get filtered log entries"""
    try:
        # Parse query parameters with defaults
        limit = min(int(request.args.get('limit', 100)), 1000)  # Cap at 1000 entries
        level = request.args.get('level')
        task_id = request.args.get('task_id')
        since = request.args.get('since')  # Expected format: YYYY-MM-DD HH:MM:SS
        
        # Convert since to datetime if provided
        since_dt = None
        if since:
            try:
                local_tz = pytz.timezone(get_timezone())
                since_dt = datetime.strptime(since, '%Y-%m-%d %H:%M:%S')
                since_dt = local_tz.localize(since_dt)
            except ValueError:
                return jsonify({
                    'error': 'Invalid date format. Expected YYYY-MM-DD HH:MM:SS'
                }), 400
        
        # Get logs with filters
        logs = get_logs(
            limit=limit,
            level=level,
            task_id=task_id,
            since=since_dt
        )
        
        return jsonify({
            'logs': logs,
            'count': len(logs)
        })
        
    except Exception as e:
        return jsonify({
            'error': str(e)
        }), 500

@logs_bp.route('/api/logs/clear', methods=['POST'])
def clear_logs():
    """Clear old logs based on age"""
    try:
        days = int(request.json.get('days', 30))
        if days < 1:
            return jsonify({
                'error': 'Days parameter must be at least 1'
            }), 400
            
        clear_old_logs(days)
        
        return jsonify({
            'message': f'Successfully cleared logs older than {days} days'
        })
        
    except Exception as e:
        return jsonify({
            'error': str(e)
        }), 500

@logs_bp.route('/api/logs/task/<int:task_id>', methods=['GET'])
def get_task_logs(task_id):
    """Get all logs for a specific task"""
    try:
        logs = get_logs(
            limit=1000,  # Higher limit for task-specific logs
            task_id=task_id
        )
        
        return jsonify({
            'task_id': task_id,
            'logs': logs,
            'count': len(logs)
        })
        
    except Exception as e:
        return jsonify({
            'error': str(e)
        }), 500

@logs_bp.route('/api/logs/stats', methods=['GET'])
def get_log_stats():
    """Get log statistics for the dashboard"""
    try:
        # Get recent logs for stats
        recent_logs = get_logs(limit=1000)
        
        # Calculate statistics
        stats = {
            'total_count': len(recent_logs),
            'by_level': {},
            'by_hour': {},
            'recent_errors': []
        }
        
        # Use configured timezone
        local_tz = pytz.timezone(get_timezone())
        now = datetime.now(local_tz)
        hour_ago = now - timedelta(hours=1)
        
        for log in recent_logs:
            # Count by level
            level = log['level']
            stats['by_level'][level] = stats['by_level'].get(level, 0) + 1
            
            # Count by hour for recent logs
            try:
                # Parse the timestamp (already localized by get_logs)
                log_time = datetime.strptime(log['timestamp'], '%Y-%m-%d %H:%M:%S')
                log_time = local_tz.localize(log_time)
                
                hour_key = log_time.strftime('%Y-%m-%d %H:00')
                stats['by_hour'][hour_key] = stats['by_hour'].get(hour_key, 0) + 1
                
                # Collect recent errors
                if level in ['ERROR', 'CRITICAL'] and log_time > hour_ago:
                    stats['recent_errors'].append({
                        'timestamp': log['timestamp'],
                        'message': log['message'],
                        'task_id': log['task_id']
                    })
            except ValueError:
                continue
        
        return jsonify(stats)
        
    except Exception as e:
        return jsonify({
            'error': str(e)
        }), 500