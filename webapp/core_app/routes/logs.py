from flask import Blueprint, jsonify, request, render_template
from datetime import datetime, timedelta
from webapp.core_app.core.log_manager import get_logs, clear_old_logs
from webapp.core_app.timezone import localize_timestamp, get_timezone
from webapp.core_app.core.database import db
import pytz

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
        processing_status = request.args.get('processing_status')  # New filter
        
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
        
        # Add processing_status to log fetching
        logs = get_logs(
            limit=limit,
            level=level,
            task_id=task_id,
            since=since_dt,
            processing_status=processing_status
        )
        
        # Explicitly ensure logs is a list
        if logs is None:
            logs = []
        
        return jsonify({
            'logs': logs,
            'count': len(logs)
        })
        
    except Exception as e:
        logger.error(f"Failed to fetch logs: {str(e)}", exc_info=True)
        return jsonify({
            'error': f'Failed to fetch logs: {str(e)}',
            'logs': [],
            'count': 0
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
        processing_status = request.args.get('processing_status')
        
        logs = get_logs(
            limit=1000,
            task_id=task_id,
            processing_status=processing_status
        )
        
        # Get task's current status and processing_status
        with db.get_connection() as conn:
            c = conn.cursor()
            c.execute("""
                SELECT status, processing_status 
                FROM tasks 
                WHERE id = ?
            """, (task_id,))
            result = c.fetchone()
            current_status = {
                'status': result[0] if result else None,
                'processing_status': result[1] if result else None
            }
        
        return jsonify({
            'task_id': task_id,
            'logs': logs,
            'count': len(logs),
            'current_status': current_status
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

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
            'by_processing_status': {},
            'recent_errors': []
        }
        
        local_tz = pytz.timezone(get_timezone())
        now = datetime.now(local_tz)
        hour_ago = now - timedelta(hours=1)
        
        for log in recent_logs:
            level = log['level']
            stats['by_level'][level] = stats['by_level'].get(level, 0) + 1
            
            if 'processing_status' in log and log['processing_status']:
                proc_status = log['processing_status']
                stats['by_processing_status'][proc_status] = \
                    stats['by_processing_status'].get(proc_status, 0) + 1
            
            try:
                log_time = datetime.strptime(log['timestamp'], '%Y-%m-%d %H:%M:%S')
                log_time = local_tz.localize(log_time)
                
                hour_key = log_time.strftime('%Y-%m-%d %H:00')
                stats['by_hour'][hour_key] = stats['by_hour'].get(hour_key, 0) + 1
                
                if level in ['ERROR', 'CRITICAL'] and log_time > hour_ago:
                    error_entry = {
                        'timestamp': log['timestamp'],
                        'message': log['message'],
                        'task_id': log['task_id']
                    }
                    if 'processing_status' in log:
                        error_entry['processing_status'] = log['processing_status']
                    stats['recent_errors'].append(error_entry)
            except ValueError:
                continue
        
        # Get current task processing stats
        with db.get_connection() as conn:
            c = conn.cursor()
            c.execute("""
                SELECT processing_status, COUNT(*) 
                FROM tasks 
                WHERE status != 'completed'
                GROUP BY processing_status
            """)
            current_tasks = dict(c.fetchall())
            stats['current_tasks'] = {
                'pending': current_tasks.get('pending', 0),
                'processed': current_tasks.get('processed', 0),
                'failed': current_tasks.get('failed', 0)
            }
        
        return jsonify(stats)
        
    except Exception as e:
        return jsonify({
            'error': str(e)
        }), 500

@logs_bp.route('/api/logs/processing', methods=['GET'])
def get_processing_logs():
    """Get logs specifically for video processing"""
    try:
        hours = int(request.args.get('hours', 24))
        if hours < 1 or hours > 168:  # Max 1 week
            return jsonify({
                'error': 'Hours must be between 1 and 168'
            }), 400
            
        since = datetime.now() - timedelta(hours=hours)
        
        logs = get_logs(
            limit=1000,
            since=since,
            processing_status=request.args.get('status')
        )
        
        # Group logs by task and status
        tasks = {}
        for log in logs:
            task_id = log.get('task_id')
            if task_id:
                if task_id not in tasks:
                    tasks[task_id] = {
                        'logs': [],
                        'processing_status': log.get('processing_status'),
                        'last_update': log['timestamp']
                    }
                tasks[task_id]['logs'].append(log)
                
        return jsonify({
            'tasks': tasks,
            'count': len(tasks)
        })
        
    except Exception as e:
        return jsonify({
            'error': str(e)
        }), 500

@logs_bp.route('/api/logs/night-processing', methods=['GET'])
def get_night_processing_logs():
    """Get logs for night processing activities"""
    try:
        days = int(request.args.get('days', 1))
        if days < 1 or days > 7:  # Max 1 week
            return jsonify({
                'error': 'Days must be between 1 and 7'
            }), 400
            
        since = datetime.now() - timedelta(days=days)
        
        # Get all processing-related logs
        logs = get_logs(
            limit=1000,
            since=since,
            processing_status=['pending', 'processed', 'failed']
        )
        
        # Group by night
        nights = {}
        local_tz = pytz.timezone(get_timezone())
        night_start = datetime.strptime(os.getenv('NIGHT_PROCESSING_START', '22:00'), '%H:%M').time()
        night_end = datetime.strptime(os.getenv('NIGHT_PROCESSING_END', '06:00'), '%H:%M').time()
        
        for log in logs:
            log_time = datetime.strptime(log['timestamp'], '%Y-%m-%d %H:%M:%S')
            log_time = local_tz.localize(log_time)
            
            # Check if log is within night processing window
            current_time = log_time.time()
            is_night = False
            if night_start < night_end:
                is_night = night_start <= current_time <= night_end
            else:  # Crosses midnight
                is_night = current_time >= night_start or current_time <= night_end
                
            if is_night:
                # Use date of the start of the night
                if current_time < night_end:
                    night_date = (log_time - timedelta(days=1)).date()
                else:
                    night_date = log_time.date()
                    
                night_key = night_date.isoformat()
                if night_key not in nights:
                    nights[night_key] = {
                        'logs': [],
                        'tasks_processed': set(),
                        'tasks_failed': set()
                    }
                    
                nights[night_key]['logs'].append(log)
                
                if log.get('task_id'):
                    if log.get('processing_status') == 'failed':
                        nights[night_key]['tasks_failed'].add(log['task_id'])
                    elif log.get('processing_status') == 'processed':
                        nights[night_key]['tasks_processed'].add(log['task_id'])
        
        # Convert sets to lists for JSON serialization
        for night in nights.values():
            night['tasks_processed'] = list(night['tasks_processed'])
            night['tasks_failed'] = list(night['tasks_failed'])
        
        return jsonify({
            'nights': nights,
            'count': len(nights)
        })
        
    except Exception as e:
        return jsonify({
            'error': str(e)
        }), 500