# webapp/app/core/log_manager.py
import json
import logging
import threading
from datetime import datetime
import pytz
from app.timezone import get_timezone
from app.core.database import db

class DatabaseLogHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.buffer = []
        self.buffer_lock = threading.Lock()
        self.flush_timer = None
        self.max_buffer = 100
        self.flush_interval = 5.0  # seconds
        
    def emit(self, record):
        """Buffer log records and flush periodically"""
        try:
            with self.buffer_lock:
                self.buffer.append(record)
                
                if len(self.buffer) >= self.max_buffer:
                    self.flush()
                elif not self.flush_timer:
                    self.flush_timer = threading.Timer(self.flush_interval, self.flush)
                    self.flush_timer.daemon = True
                    self.flush_timer.start()
        except Exception as e:
            print(f"Error in log handler: {e}")
    
    def flush(self):
        """Flush buffered records to database"""
        if not self.buffer:
            return
            
        try:
            with self.buffer_lock:
                records = self.buffer[:]
                self.buffer.clear()
                
            if self.flush_timer:
                self.flush_timer.cancel()
                self.flush_timer = None
                
            with db.get_connection() as conn:
                c = conn.cursor()
                for record in records:
                    current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')
                    task_id = getattr(record, 'task_id', None)
                    
                    c.execute('''
                        INSERT INTO logs (timestamp, level, message, task_id, details, source)
                        VALUES (?, ?, ?, ?, ?, ?)
                    ''', (
                        current_time,
                        record.levelname,
                        record.getMessage(),
                        task_id,
                        json.dumps(getattr(record, 'details', None)) if hasattr(record, 'details') else None,
                        record.module
                    ))
                conn.commit()
                
        except Exception as e:
            print(f"Error flushing log buffer: {e}")

# Create handler instance
db_log_handler = DatabaseLogHandler()

def add_log_entry(level, message, task_id=None, details=None, source=None):
    """Add a new log entry to the database with retries"""
    try:
        with db.get_connection() as conn:
            c = conn.cursor()
            current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')
            
            c.execute('''
                INSERT INTO logs (timestamp, level, message, task_id, details, source)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (
                current_time,
                level,
                message,
                task_id,
                json.dumps(details) if details else None,
                source
            ))
            conn.commit()
            
    except Exception as e:
        print(f"Failed to add log entry: {str(e)}")

def get_logs(limit=100, level=None, task_id=None, since=None, processing_status=None):
    """Retrieve logs with optional filtering"""
    try:
        with db.get_connection() as conn:
            c = conn.cursor()
            
            query = "SELECT * FROM logs"
            params = []
            conditions = []
            
            if level:
                conditions.append("level = ?")
                params.append(level)
                
            if task_id:
                conditions.append("task_id = ?")
                params.append(task_id)
                
            if since:
                local_tz = pytz.timezone(get_timezone())
                utc_since = local_tz.localize(since).astimezone(pytz.UTC)
                conditions.append("timestamp > ?")
                params.append(utc_since.strftime('%Y-%m-%d %H:%M:%S'))
            
            if processing_status:
                if isinstance(processing_status, str):
                    conditions.append("task_id IN (SELECT id FROM tasks WHERE processing_status = ?)")
                    params.append(processing_status)
                elif isinstance(processing_status, list):
                    placeholders = ','.join(['?' for _ in processing_status])
                    conditions.append(f"task_id IN (SELECT id FROM tasks WHERE processing_status IN ({placeholders}))")
                    params.extend(processing_status)
            
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            
            query += " ORDER BY timestamp DESC LIMIT ?"
            params.append(limit)
            
            c.execute(query, params)
            logs = c.fetchall()
            
            return [{
                'id': log[0],
                'timestamp': datetime.strptime(log[1], '%Y-%m-%d %H:%M:%S.%f')
                    .replace(tzinfo=pytz.UTC)
                    .astimezone(pytz.timezone(get_timezone()))
                    .strftime('%Y-%m-%d %H:%M:%S.%f')[:-3],
                'level': log[2],
                'message': log[3],
                'task_id': log[4],
                'details': json.loads(log[5]) if log[5] else None,
                'source': log[6]
            } for log in logs]
            
    except Exception as e:
        print(f"Failed to retrieve logs: {e}")
        raise

def clear_old_logs(days=30):
    """Clear logs older than specified days"""
    try:
        with db.get_connection() as conn:
            c = conn.cursor()
            c.execute('''
                DELETE FROM logs 
                WHERE timestamp < datetime('now', '-? days')
            ''', (days,))
            conn.commit()
            
    except Exception as e:
        print(f"Failed to clear old logs: {e}")