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
                conn.execute("PRAGMA busy_timeout=30000")  # 30-second timeout
                conn.execute("PRAGMA cache_size=-10000")   # 10MB cache
                conn.execute("PRAGMA temp_store=MEMORY")
                conn.execute("PRAGMA mmap_size=30000000000")  # 30GB memory map
                conn.execute("PRAGMA page_size=4096")
                
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
                    
                    -- Ensure a single lock row exists
                    INSERT OR IGNORE INTO task_lock (id, locked) VALUES (1, 0);
                ''')
                
                logger.info("Database initialized with optimized settings")
        except sqlite3.Error as e:
            logger.error(f"Database initialization error: {e}")
            raise
    
    def reset_db_locks(self):
        """Reset all database locks in case of deadlock"""
        try:
            with self.get_connection() as conn:
                conn.execute("""
                    UPDATE task_lock 
                    SET locked = 0, 
                        task_id = NULL, 
                        locked_at = NULL
                """)
                logger.info("Database locks have been reset")
        except Exception as e:
            logger.error(f"Failed to reset database locks: {e}")

    def vacuum_db(self):
        """Optimize database and reclaim space"""
        try:
            with self.get_connection() as conn:
                conn.execute("VACUUM")
                logger.info("Database vacuum completed")
        except Exception as e:
            logger.error(f"Failed to vacuum database: {e}")
    
    @contextlib.contextmanager
    def get_connection(self, max_retries=5, retry_delay=1.0):
        """
        Get a database connection with robust error handling and retry mechanism
        
        :param max_retries: Number of times to retry on database lock
        :param retry_delay: Initial delay between retries (will be exponentially increased)
        :return: A database connection
        """
        last_error = None
        conn = None
        for attempt in range(max_retries):
            try:
                # Open connection with extended timeout and WAL mode
                conn = sqlite3.connect(
                    get_db_path(), 
                    timeout=60.0,  # 60-second timeout
                    isolation_level=None,  # Autocommit mode
                    check_same_thread=False  # Allow cross-thread access
                )
                
                # Set pragmas for performance and concurrency
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA busy_timeout=30000")  # 30-second busy timeout
                conn.execute("PRAGMA temp_store=MEMORY")
                conn.execute("PRAGMA mmap_size=30000000000")  # 30GB memory map
                conn.execute("PRAGMA page_size=4096")
                
                yield conn
                return
                
            except sqlite3.OperationalError as e:
                if conn:
                    try:
                        conn.close()
                    except:
                        pass
                    
                logger.warning(f"Database connection attempt {attempt + 1} failed: {e}")
                last_error = e
                
                if "database is locked" in str(e) and attempt == max_retries - 1:
                    try:
                        self.reset_db_locks()
                    except:
                        pass
                
                time.sleep(retry_delay * (2 ** attempt))
            
            except Exception as e:
                if conn:
                    try:
                        conn.close()
                    except:
                        pass
                raise
            
            finally:
                if conn:
                    try:
                        conn.close()
                    except Exception as e:
                        logger.error(f"Error closing connection: {e}")
        
        # If all retries fail
        logger.error(f"Failed to acquire database connection after {max_retries} attempts")
        if last_error:
            raise last_error
        raise sqlite3.OperationalError("Unknown database connection error")
    
    def with_connection(self, f):
        """Decorator to handle database connections"""
        @wraps(f)
        def wrapper(*args, **kwargs):
            with self.get_connection() as conn:
                return f(conn, *args, **kwargs)
        return wrapper

# Create the singleton instance
db = DatabaseManager.get_instance()