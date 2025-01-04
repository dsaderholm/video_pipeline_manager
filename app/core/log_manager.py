import os
import json
import sqlite3
from datetime import datetime
from app import logger

# Maximum number of log entries to keep in the database
MAX_LOG_ENTRIES = 1000

def init_logs():
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
            
        logger.info("Log system initialized")
        
    except Exception as e:
        logger.error(f"Failed to initialize log system: {str(e)}")
        raise

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
            
            # Delete old entries if we exceed MAX_LOG_ENTRIES
            c.execute(f'''
                DELETE FROM logs 
                WHERE id NOT IN (
                    SELECT id FROM logs 
                    ORDER BY timestamp DESC 
                    LIMIT {MAX_LOG_ENTRIES}
                )
            ''')
            
            conn.commit()
            
    except Exception as e:
        logger.error(f"Failed to add log entry: {str(e)}")

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
                conditions.append("timestamp > ?")
                params.append(since)
            
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            
            query += " ORDER BY timestamp DESC LIMIT ?"
            params.append(limit)
            
            c.execute(query, params)
            logs = c.fetchall()
            
            # Convert to list of dictionaries
            return [{
                'id': log[0],
                'timestamp': log[1],
                'level': log[2],
                'message': log[3],
                'task_id': log[4],
                'details': json.loads(log[5]) if log[5] else None,
                'source': log[6]
            } for log in logs]
            
    except Exception as e:
        logger.error(f"Failed to retrieve logs: {str(e)}")
        return []

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
        logger.error(f"Failed to clear old logs: {str(e)}")

class DatabaseLogHandler:
    """Custom log handler that saves logs to the database"""
    
    def __init__(self):
        self.initialized = False
        
    def initialize(self):
        """Initialize the log system"""
        if not self.initialized:
            init_logs()
            self.initialized = True
    
    def emit(self, level, message, task_id=None, details=None, source=None):
        """Emit a log message to the database"""
        if not self.initialized:
            self.initialize()
        add_log_entry(level, message, task_id, details, source)

# Create global log handler instance
db_log_handler = DatabaseLogHandler()
