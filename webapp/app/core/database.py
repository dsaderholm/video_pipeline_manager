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
    _connection_pool = {}
    
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
            with self.get_connection() as conn:
                # Set WAL mode and optimize for concurrency
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA busy_timeout=60000")  # Increased to 60-second timeout
                conn.execute("PRAGMA cache_size=-64000")   # Increased to 64MB cache
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

    def _get_connection_key(self) -> str:
        """Get unique key for current thread"""
        return f"thread_{threading.get_ident()}"

    def _create_connection(self) -> sqlite3.Connection:
        """Create a new database connection with optimized settings"""
        conn = sqlite3.connect(
            get_db_path(),
            timeout=60.0,  # 60-second connection timeout
            isolation_level=None,  # Autocommit mode
            check_same_thread=False  # Allow cross-thread access
        )
        
        # Enable WAL mode for this connection
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=60000")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA cache_size=-64000")
        
        return conn

    @contextlib.contextmanager
    def get_connection(self, max_retries: int = 5, initial_retry_delay: float = 0.1):
        """
        Get a database connection with exponential backoff retry
        
        Args:
            max_retries: Maximum number of retry attempts
            initial_retry_delay: Initial delay between retries (doubles each attempt)
        """
        conn_key = self._get_connection_key()
        conn = None
        last_error = None
        
        for attempt in range(max_retries):
            try:
                # Try to get or create connection
                if conn_key not in self._connection_pool:
                    self._connection_pool[conn_key] = self._create_connection()
                
                conn = self._connection_pool[conn_key]
                
                # Test connection
                conn.execute("SELECT 1")
                
                yield conn
                return
                
            except sqlite3.OperationalError as e:
                delay = initial_retry_delay * (2 ** attempt)  # Exponential backoff
                
                if "database is locked" in str(e):
                    logger.warning(f"Database locked, attempt {attempt + 1}/{max_retries}. Retrying in {delay:.2f}s")
                    time.sleep(delay)
                    last_error = e
                    
                    # On last attempt, try to reset locks
                    if attempt == max_retries - 1:
                        try:
                            self.reset_db_locks()
                        except:
                            pass
                    continue
                    
                logger.error(f"Database error on attempt {attempt + 1}: {e}")
                raise
                
            except Exception as e:
                logger.error(f"Unexpected database error: {e}")
                raise
                
            finally:
                if attempt == max_retries - 1 and conn_key in self._connection_pool:
                    # Close and remove connection on last attempt
                    try:
                        self._connection_pool[conn_key].close()
                    except:
                        pass
                    del self._connection_pool[conn_key]
        
        if last_error:
            raise last_error
        raise sqlite3.OperationalError("Failed to acquire database connection")

    def with_connection(self, f):
        """Decorator to handle database connections"""
        @wraps(f)
        def wrapper(*args, **kwargs):
            with self.get_connection() as conn:
                return f(conn, *args, **kwargs)
        return wrapper

    def cleanup(self):
        """Clean up all database connections"""
        for conn_key, conn in self._connection_pool.items():
            try:
                conn.close()
            except:
                pass
        self._connection_pool.clear()

# Create the singleton instance
db = DatabaseManager.get_instance()