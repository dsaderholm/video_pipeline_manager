import sqlite3
import threading
import contextlib
from functools import wraps
import os
import logging
import time

logger = logging.getLogger('app')

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
        """Initialize database with improved settings"""
        try:
            with sqlite3.connect(get_db_path()) as conn:
                # Set WAL mode and optimize for concurrency
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA busy_timeout=10000")  # 10-second timeout
                conn.execute("PRAGMA cache_size=-10000")   # 10MB cache
                
                # Create tables if they don't exist (add any additional tables)
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
                    
                    -- Ensure a single lock row exists
                    INSERT OR IGNORE INTO task_lock (id, locked) VALUES (1, 0);
                ''')
                
                logger.info("Database initialized with optimized settings")
        except sqlite3.Error as e:
            logger.error(f"Database initialization error: {e}")
            raise
    
    @contextlib.contextmanager
    def get_connection(self, max_retries=3):
        """
        Get a database connection with robust error handling and retry mechanism
        
        :param max_retries: Number of times to retry on database lock
        :return: A database connection
        """
        last_error = None
        for attempt in range(max_retries):
            try:
                # Open connection with extended timeout and WAL mode
                conn = sqlite3.connect(
                    get_db_path(), 
                    timeout=30.0,  # 30-second timeout
                    isolation_level=None,  # Autocommit mode
                    check_same_thread=False  # Allow cross-thread access
                )
                
                # Set pragmas for performance and concurrency
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA busy_timeout=10000")  # 10-second busy timeout
                
                try:
                    yield conn
                finally:
                    conn.close()
                
                # If we get here, connection was successful
                return
            
            except sqlite3.OperationalError as e:
                logger.warning(f"Database connection attempt {attempt + 1} failed: {e}")
                last_error = e
                
                # Exponential backoff
                time.sleep(0.1 * (2 ** attempt))
        
        # If all retries fail
        logger.error(f"Failed to acquire database connection after {max_retries} attempts")
        raise last_error if last_error else sqlite3.OperationalError("Unknown database connection error")
    
    def with_connection(self, f):
        """Decorator to handle database connections"""
        @wraps(f)
        def wrapper(*args, **kwargs):
            with self.get_connection() as conn:
                return f(conn, *args, **kwargs)
        return wrapper

# Create the singleton instance
db = DatabaseManager.get_instance()