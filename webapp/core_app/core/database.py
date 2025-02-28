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
    db_dir = os.path.join('webapp', 'database')
    # Make sure the database directory exists
    if not os.path.exists(db_dir):
        os.makedirs(db_dir)
    return os.path.join(db_dir, 'pipeline.db')

class DatabaseManager:
    _instance = None
    _lock = threading.Lock()
    _connection_pool = {}
    _in_transaction = {}  # Track which connections are in transactions
    
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
        db_path = get_db_path()
        logger.info(f"Initializing database at {db_path}")
        
        # Create database directory if it doesn't exist
        db_dir = os.path.dirname(db_path)
        if not os.path.exists(db_dir):
            os.makedirs(db_dir)
            logger.info(f"Created database directory: {db_dir}")
            
        try:
            with self.get_connection() as conn:
                # Set WAL mode and optimize for concurrency
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA busy_timeout=120000")  # Increased to 120-second timeout
                conn.execute("PRAGMA cache_size=-64000")    # 64MB cache
                conn.execute("PRAGMA temp_store=MEMORY")
                conn.execute("PRAGMA mmap_size=30000000000") # 30GB memory map
                conn.execute("PRAGMA page_size=4096")
                conn.execute("PRAGMA wal_autocheckpoint=1000") # Increase checkpoint threshold
                
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
                    
                    -- Create generators table if it doesn't exist
                    CREATE TABLE IF NOT EXISTS generators (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL,
                        generator_curl TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                ''')
                
                logger.info("Database initialized with optimized settings")
        except sqlite3.Error as e:
            logger.error(f"Database initialization error: {e}")
            raise
    
    def reset_db_locks(self):
        """Reset all database locks in case of deadlock"""
        try:
            # Create a fresh connection for this operation to avoid using potentially closed connections
            conn = self._create_connection()
            conn.execute("""
                UPDATE task_lock 
                SET locked = 0, 
                    task_id = NULL, 
                    locked_at = NULL
            """)
            conn.commit()
            conn.close()
            logger.info("Database locks have been reset")
        except Exception as e:
            logger.error(f"Failed to reset database locks: {e}")

    def vacuum_db(self):
        """Optimize database and reclaim space"""
        try:
            # Create a fresh connection for this operation
            conn = self._create_connection()
            conn.execute("VACUUM")
            conn.commit()
            conn.close()
            logger.info("Database vacuum completed")
        except Exception as e:
            logger.error(f"Failed to vacuum database: {e}")

    def _get_connection_key(self) -> str:
        """Get unique key for current thread"""
        return f"thread_{threading.get_ident()}"

    def _create_connection(self) -> sqlite3.Connection:
        """Create a new database connection with optimized settings"""
        db_path = get_db_path()
        
        # Ensure the database directory exists
        db_dir = os.path.dirname(db_path)
        if not os.path.exists(db_dir):
            os.makedirs(db_dir)
            
        conn = sqlite3.connect(
            db_path,
            timeout=120.0,  # 120-second connection timeout
            isolation_level=None,  # Autocommit mode
            check_same_thread=False  # Allow cross-thread access
        )
        
        # Enable WAL mode for this connection
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=120000")  # 120-second timeout
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
                # Check if connection exists and is valid
                if conn_key in self._connection_pool:
                    try:
                        # Test existing connection
                        self._connection_pool[conn_key].cursor().execute("SELECT 1")
                    except (sqlite3.ProgrammingError, sqlite3.OperationalError):
                        # Connection is invalid, remove it from pool
                        logger.warning(f"Detected invalid connection in pool. Creating new connection.")
                        try:
                            self._connection_pool[conn_key].close()
                        except:
                            pass
                        del self._connection_pool[conn_key]
                        if conn_key in self._in_transaction:
                            del self._in_transaction[conn_key]
                
                # Create new connection if needed
                if conn_key not in self._connection_pool:
                    self._connection_pool[conn_key] = self._create_connection()
                    self._in_transaction[conn_key] = False
                
                conn = self._connection_pool[conn_key]
                
                # Test connection
                conn.cursor().execute("SELECT 1")
                
                # We will track transactions through the execute_transaction method instead
                # of monkey-patching the connection object
                
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
                # Force recreation of connection
                if conn_key in self._connection_pool:
                    try:
                        self._connection_pool[conn_key].close()
                    except:
                        pass
                    del self._connection_pool[conn_key]
                    if conn_key in self._in_transaction:
                        del self._in_transaction[conn_key]
                raise
                
            except Exception as e:
                logger.error(f"Unexpected database error: {e}")
                # Force recreation of connection
                if conn_key in self._connection_pool:
                    try:
                        self._connection_pool[conn_key].close()
                    except:
                        pass
                    del self._connection_pool[conn_key]
                    if conn_key in self._in_transaction:
                        del self._in_transaction[conn_key]
                raise
        
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
                # Check if transaction is active and try to rollback
                if conn_key in self._in_transaction and self._in_transaction[conn_key]:
                    try:
                        conn.execute("ROLLBACK")
                    except:
                        pass
                conn.close()
            except:
                pass
        self._connection_pool.clear()
        self._in_transaction.clear()

    def checkpoint_wal(self):
        """Force a WAL checkpoint to prevent the WAL file from growing too large"""
        conn = None
        try:
            conn = self._create_connection()
            conn.execute("PRAGMA wal_checkpoint(FULL)")
            conn.close()
            logger.info("WAL checkpoint completed successfully")
        except Exception as e:
            logger.error(f"WAL checkpoint failed: {e}")
            if conn:
                try:
                    conn.close()
                except:
                    pass
                    
    def begin_transaction(self, conn):
        """Mark the connection as being in a transaction"""
        conn_key = self._get_connection_key()
        conn.execute("BEGIN IMMEDIATE")
        self._in_transaction[conn_key] = True

    def commit_transaction(self, conn):
        """Commit the transaction and mark the connection as not in a transaction"""
        conn_key = self._get_connection_key()
        conn.commit()
        self._in_transaction[conn_key] = False

    def rollback_transaction(self, conn):
        """Rollback the transaction and mark the connection as not in a transaction"""
        conn_key = self._get_connection_key()
        conn.rollback()
        self._in_transaction[conn_key] = False

# Create the singleton instance
db = DatabaseManager.get_instance()