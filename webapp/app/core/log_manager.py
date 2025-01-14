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
                
                # Create logs table if it doesn't exist
                c.execute('''
                    CREATE TABLE IF NOT EXISTS logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
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
                
                # Get latest sequence number for this second
                second_start = current_time[:19]  # Strip microseconds
                c.execute("""
                    SELECT COALESCE(MAX(sequence), 0)
                    FROM logs
                    WHERE timestamp >= ? AND timestamp < datetime(?, '+1 second')
                """, (second_start, second_start))
                
                sequence = c.fetchone()[0] + 1
                
                # Add new log entry
                c.execute('''
                    INSERT INTO logs (timestamp, sequence, level, message, task_id, details, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (
                    current_time,
                    sequence,
                    record.levelname, 
                    record.getMessage(), 
                    getattr(record, 'task_id', None),
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
            
            # Order by timestamp and sequence number
            query += " ORDER BY timestamp DESC, sequence DESC LIMIT ?"
            params.append(limit)
            
            c.execute(query, params)
            logs = c.fetchall()
            
            # Convert to list of dictionaries with microsecond precision
            return [{
                'id': log[0],
                'timestamp': datetime.strptime(log[1], '%Y-%m-%d %H:%M:%S.%f')
                    .replace(tzinfo=pytz.UTC)
                    .astimezone(pytz.timezone(get_timezone()))
                    .strftime('%Y-%m-%d %H:%M:%S.%f')[:-3],  # Truncate to milliseconds
                'level': log[3],
                'message': log[4],
                'task_id': log[5],
                'details': json.loads(log[6]) if log[6] else None,
                'source': log[7]
            } for log in logs]
            
    except Exception as e:
        print(f"Failed to retrieve logs: {str(e)}")
        return []

def add_log_entry(level, message, task_id=None, details=None, source=None):
    """Add a new log entry to the database"""
    try:
        with sqlite3.connect('pipeline.db') as conn:
            c = conn.cursor()
            
            # Add new log entry
            c.execute('''
                INSERT INTO logs (level, message, task_id, details, source)
                VALUES (?, ?, ?, ?, ?)
            ''', (level, message, task_id, 
                 json.dumps(details) if details else None,
                 source))
            
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
            
            # Create logs table if it doesn't exist
            c.execute('''
                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,  -- Changed from TIMESTAMP to TEXT to store microseconds
                    sequence INTEGER NOT NULL,  -- Added sequence number within the same second
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