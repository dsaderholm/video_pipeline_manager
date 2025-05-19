import psycopg2
from psycopg2 import pool
import threading
import contextlib
from functools import wraps
import os
import logging
import time

logger = logging.getLogger('app')

def get_db_connection_string():
    """Get PostgreSQL connection string from environment or use default"""
    return os.environ.get('DATABASE_URL', 'postgresql://postgres:postgres@db:5432/video_pipeline')

class DatabaseManager:
    _instance = None
    _lock = threading.Lock()
    _connection_pool = None
    _local = threading.local()  # Thread-local storage for connection tracking
    
    def __init__(self):
        self._setup_connection_pool()
        self.init_db()
    
    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance
    
    def _setup_connection_pool(self):
        """Set up the PostgreSQL connection pool"""
        try:
            db_url = get_db_connection_string()
            # Wait for the PostgreSQL server to start up
            max_retries = 10
            retry_delay = 2  # seconds
            
            for attempt in range(max_retries):
                try:
                    # Create a temporary connection to test if PostgreSQL is ready
                    conn = psycopg2.connect(db_url)
                    conn.close()
                    break  # If we get here, connection was successful
                except psycopg2.OperationalError as e:
                    if attempt < max_retries - 1:
                        logger.warning(f"Waiting for PostgreSQL to start (attempt {attempt+1}/{max_retries}): {e}")
                        time.sleep(retry_delay)
                    else:
                        logger.error(f"Failed to connect to PostgreSQL after {max_retries} attempts: {e}")
                        raise
                    
            # Initialize the connection pool with 5 min and 20 max connections
            self._connection_pool = psycopg2.pool.ThreadedConnectionPool(
                minconn=5,
                maxconn=20,
                dsn=db_url
            )
            logger.info("PostgreSQL connection pool initialized")
        except Exception as e:
            logger.error(f"Failed to initialize connection pool: {e}")
            raise
    
    def init_db(self):
        """Initialize database tables"""
        try:
            with self.get_connection() as conn:
                with conn.cursor() as cursor:
                    # Create tables if they don't exist
                    cursor.execute('''
                        CREATE TABLE IF NOT EXISTS task_lock (
                            id INTEGER PRIMARY KEY CHECK (id = 1),
                            locked INTEGER DEFAULT 0,
                            task_id INTEGER,
                            locked_at TIMESTAMP
                        )
                    ''')
                    
                    # Ensure a single lock row exists
                    cursor.execute('''
                        INSERT INTO task_lock (id, locked) 
                        VALUES (1, 0) 
                        ON CONFLICT (id) DO NOTHING
                    ''')
                    
                    # Create generators table if it doesn't exist
                    cursor.execute('''
                        CREATE TABLE IF NOT EXISTS generators (
                            id SERIAL PRIMARY KEY,
                            name TEXT NOT NULL,
                            generator_curl TEXT NOT NULL,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    ''')
                    
                    # Create utilities table
                    cursor.execute('''
                        CREATE TABLE IF NOT EXISTS utilities (
                            id SERIAL PRIMARY KEY,
                            name TEXT NOT NULL,
                            utility_curl TEXT NOT NULL,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    ''')
                    
                    # Create platforms table
                    cursor.execute('''
                        CREATE TABLE IF NOT EXISTS platforms (
                            id SERIAL PRIMARY KEY,
                            name TEXT NOT NULL,
                            uploader_curl TEXT NOT NULL,
                            fallback_curl TEXT,
                            fallback_curl_2 TEXT,
                            default_hashtags TEXT,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    ''')
                    
                    # Create tasks table
                    cursor.execute('''
                        CREATE TABLE IF NOT EXISTS tasks (
                            id SERIAL PRIMARY KEY,
                            name TEXT NOT NULL,
                            generator_id INTEGER,
                            utilities TEXT,
                            schedule TEXT NOT NULL,
                            hashtags TEXT,
                            sound_name TEXT,
                            sound_volume TEXT DEFAULT 'background',
                            status TEXT DEFAULT 'pending',
                            email_notify TEXT,
                            retry_count INTEGER DEFAULT 0,
                            processing_status TEXT DEFAULT 'pending',
                            processed_video_path TEXT,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            FOREIGN KEY(generator_id) REFERENCES generators(id)
                        )
                    ''')
                    
                    # Create task_platform_accounts junction table
                    cursor.execute('''
                        CREATE TABLE IF NOT EXISTS task_platform_accounts (
                            id SERIAL PRIMARY KEY,
                            task_id INTEGER NOT NULL,
                            platform_id INTEGER NOT NULL,
                            account_name TEXT NOT NULL,
                            FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE,
                            FOREIGN KEY(platform_id) REFERENCES platforms(id) ON DELETE CASCADE
                        )
                    ''')
                    
                    # Create generated_videos table
                    cursor.execute('''
                        CREATE TABLE IF NOT EXISTS generated_videos (
                            id SERIAL PRIMARY KEY,
                            task_id INTEGER NOT NULL,
                            original_name TEXT NOT NULL,
                            processed_path TEXT NOT NULL,
                            scheduled_time TEXT NOT NULL,
                            status TEXT DEFAULT 'pending',
                            upload_status TEXT DEFAULT 'pending',
                            error_message TEXT,
                            retry_count INTEGER DEFAULT 0,
                            generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            uploaded_at TIMESTAMP,
                            FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
                        )
                    ''')
                conn.commit()
                logger.info("Database tables initialized successfully")
        except Exception as e:
            logger.error(f"Database initialization error: {e}")
            raise
    
    def reset_db_locks(self):
        """Reset all database locks in case of deadlock"""
        try:
            with self.get_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        UPDATE task_lock 
                        SET locked = 0, 
                            task_id = NULL, 
                            locked_at = NULL
                    """)
                conn.commit()
            logger.info("Database locks have been reset")
        except Exception as e:
            logger.error(f"Failed to reset database locks: {e}")
    
    def vacuum_db(self):
        """Optimize database and reclaim space"""
        try:
            with self.get_connection() as conn:
                # Set autocommit mode required for VACUUM
                old_isolation = conn.isolation_level
                conn.set_isolation_level(0)  # AUTOCOMMIT isolation level
                
                with conn.cursor() as cursor:
                    cursor.execute("VACUUM ANALYZE")
                
                # Reset isolation level
                conn.set_isolation_level(old_isolation)
            logger.info("Database vacuum completed")
        except Exception as e:
            logger.error(f"Failed to vacuum database: {e}")

    @contextlib.contextmanager
    def get_connection(self, max_retries: int = 5, initial_retry_delay: float = 0.1):
        """
        Get a database connection from the pool with exponential backoff retry
        
        Args:
            max_retries: Maximum number of retry attempts
            initial_retry_delay: Initial delay between retries (doubles each attempt)
        """
        conn = None
        last_error = None
        
        for attempt in range(max_retries):
            try:
                # Get connection from pool
                conn = self._connection_pool.getconn()
                
                # Verify connection is alive
                with conn.cursor() as cursor:
                    cursor.execute("SELECT 1")
                    result = cursor.fetchone()
                    if result is None or result[0] != 1:
                        raise psycopg2.OperationalError("Connection health check failed")
                
                # Store thread ID to track this connection in thread-local storage
                self._local.thread_id = threading.get_ident()
                self._local.connection = conn
                
                try:
                    yield conn
                finally:
                    # Return connection to the pool
                    try:
                        self._connection_pool.putconn(conn)
                        # Clear thread-local storage
                        if hasattr(self._local, 'connection'):
                            self._local.connection = None
                    except Exception as e:
                        logger.error(f"Error returning connection to pool: {e}")
                return
            
            except psycopg2.OperationalError as e:
                last_error = e
                delay = initial_retry_delay * (2 ** attempt)  # Exponential backoff
                logger.warning(f"Database connection error, attempt {attempt + 1}/{max_retries}. Retrying in {delay:.2f}s: {e}")
                time.sleep(delay)
                
                # On last attempt, try to reset locks
                if attempt == max_retries - 1 and conn is not None:
                    try:
                        self.reset_db_locks()
                    except:
                        pass
                        
                # If we got a connection but it failed, return it to the pool
                if conn is not None:
                    try:
                        self._connection_pool.putconn(conn, close=True)  # Force close this connection
                        # Clear thread-local storage
                        if hasattr(self._local, 'connection'):
                            self._local.connection = None
                    except:
                        pass
                    conn = None
                        
            except Exception as e:
                logger.error(f"Unexpected database error: {e}")
                # If we got a connection but it failed, return it to the pool
                if conn is not None:
                    try:
                        self._connection_pool.putconn(conn, close=True)  # Force close this connection
                        # Clear thread-local storage
                        if hasattr(self._local, 'connection'):
                            self._local.connection = None
                    except:
                        pass
                raise
        
        if last_error:
            raise last_error
        raise psycopg2.OperationalError("Failed to acquire database connection")

    def with_connection(self, f):
        """Decorator to handle database connections"""
        @wraps(f)
        def wrapper(*args, **kwargs):
            with self.get_connection() as conn:
                return f(conn, *args, **kwargs)
        return wrapper

    def create_connection(self):
        """Create a new connection directly (not from the pool) for special operations"""
        try:
            db_url = get_db_connection_string()
            conn = psycopg2.connect(db_url)
            return conn
        except Exception as e:
            logger.error(f"Failed to create direct connection: {e}")
            raise
            
    def cleanup(self):
        """Clean up database connection pool"""
        if self._connection_pool is not None:
            self._connection_pool.closeall()
            logger.info("Database connection pool closed")

    def begin_transaction(self, conn):
        """Begin a transaction"""
        conn.autocommit = False

    def commit_transaction(self, conn):
        """Commit a transaction"""
        conn.commit()
        conn.autocommit = True

    def rollback_transaction(self, conn):
        """Rollback a transaction"""
        conn.rollback()
        conn.autocommit = True

# Create the singleton instance
db = DatabaseManager.get_instance()