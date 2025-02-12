# webapp/app/core/database.py
import sqlite3
import threading
import contextlib
from functools import wraps
import os

def get_db_path():
    """Get the path to the SQLite database file"""
    return os.path.join('webapp', 'database', 'pipeline.db')

class DatabaseManager:
    _instance = None
    _lock = threading.Lock()
    
    def __init__(self):
        self.init_db()
    
    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance
    
    def init_db(self):
        """Initialize database with proper settings"""
        with sqlite3.connect(get_db_path()) as conn:
            # Set WAL mode permanently
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=60000")
            
            # Create tables if they don't exist
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    task_id INTEGER,
                    details TEXT,
                    source TEXT
                );
                
                CREATE TABLE IF NOT EXISTS task_lock (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    locked INTEGER DEFAULT 0,
                    task_id INTEGER,
                    locked_at TEXT
                );
                
                -- Insert the single lock row if it doesn't exist
                INSERT OR IGNORE INTO task_lock (id, locked) VALUES (1, 0);
            ''')
    
    @contextlib.contextmanager
    def get_connection(self):
        """Get a database connection with proper settings"""
        conn = sqlite3.connect(get_db_path(), timeout=60.0)
        try:
            conn.execute("PRAGMA busy_timeout=60000")
            yield conn
        finally:
            conn.close()
    
    def with_connection(f):
        """Decorator to handle database connections"""
        @wraps(f)
        def wrapper(*args, **kwargs):
            with DatabaseManager.get_instance().get_connection() as conn:
                return f(conn, *args, **kwargs)
        return wrapper

# Create the singleton instance
db = DatabaseManager.get_instance()