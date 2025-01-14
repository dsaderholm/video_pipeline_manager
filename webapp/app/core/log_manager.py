import os
import json
import sqlite3
import logging
from datetime import datetime
import pytz
from app.timezone import get_timezone

class DatabaseLogHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.initialize()

    def initialize(self):
        """Initialize the logs table in the database"""
        try:
            with sqlite3.connect('pipeline.db') as conn:
                c = conn.cursor()
                
                # Drop the existing logs table if it exists
                c.execute("DROP TABLE IF EXISTS logs")
                
                # Create logs table with new schema
                c.execute('''
                    CREATE TABLE IF NOT EXISTS logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TEXT NOT NULL,
                        level TEXT NOT NULL,
                        message TEXT NOT NULL,
                        task_id INTEGER,
                        details TEXT,
                        source TEXT
                    )
                ''')
                conn.commit()
            
            print("Log system initialized")
            
        except Exception as e:
            print(f"Failed to initialize log system: {str(e)}")
            raise

    def emit(self, record):
        """Add a log record to the database"""
        try:
            with sqlite3.connect('pipeline.db') as conn:
                c = conn.cursor()
                
                # Get current timestamp with microseconds
                current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')
                
                # Get task_id from extra if it exists
                task_id = record.__dict__.get('task_id')
                if not task_id and hasattr(record, 'extra'):
                    task_id = record.extra.get('task_id')
                
                # Add new log entry
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
            print(f"Failed to add log entry: {str(e)}")

# Create a single instance of the database log handler
db_log_handler = DatabaseLogHandler()

def get_logs(limit=100, level=None, task_id=None, since=None):
    """Retrieve logs with optional filtering"""
    try:
        with sqlite3.connect('pipeline.db') as conn:
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
            
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            
            # Order by timestamp only
            query += " ORDER BY timestamp DESC LIMIT ?"
            params.append(limit)
            
            c.execute(query, params)
            logs = c.fetchall()
            
            # Convert to list of dictionaries
            return [{
                'id': log[0],
                'timestamp': datetime.strptime(log[1], '%Y-%m-%d %H:%M:%S.%f')
                    .replace(tzinfo=pytz.UTC)
                    .astimezone(pytz.timezone(get_timezone()))
                    .strftime('%Y-%m-%d %H:%M:%S.%f')[:-3],  # Truncate to milliseconds
                'level': log[2],
                'message': log[3],
                'task_id': log[4],
                'details': json.loads(log[5]) if log[5] else None,
                'source': log[6]
            } for log in logs]
            
    except Exception as e:
        print(f"Failed to retrieve logs: {str(e)}")
        return []

def add_log_entry(level, message, task_id=None, details=None, source=None):
    """Add a new log entry to the database"""
    try:
        with sqlite3.connect('pipeline.db') as conn:
            c = conn.cursor()
            
            current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')
            
            # Add new log entry
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

def clear_old_logs(days=30):
    """Clear logs older than specified days"""
    try:
        with sqlite3.connect('pipeline.db') as conn:
            c = conn.cursor()
            c.execute('''
                DELETE FROM logs 
                WHERE timestamp < datetime('now', '-? days')
            ''', (days,))
            conn.commit()
            
    except Exception as e:
        print(f"Failed to clear old logs: {str(e)}")

def init_logs():
    """Initialize the logs table in the database"""
    try:
        with sqlite3.connect('pipeline.db') as conn:
            c = conn.cursor()
            
            # Drop the existing logs table if it exists
            c.execute("DROP TABLE IF EXISTS logs")
            
            # Create logs table with new schema
            c.execute('''
                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    task_id INTEGER,
                    details TEXT,
                    source TEXT
                )
            ''')
            conn.commit()
            
        print("Log system initialized")
        
    except Exception as e:
        print(f"Failed to initialize log system: {str(e)})")
        raise