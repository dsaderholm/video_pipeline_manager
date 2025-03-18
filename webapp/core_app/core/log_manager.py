import json
import logging
import threading
import queue
import time
from datetime import datetime
import pytz
from webapp.core_app.timezone import get_timezone
from webapp.core_app.core.database import db
import sqlite3

class DatabaseLogHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.log_queue = queue.Queue()
        self.worker_thread = None
        self.stop_event = threading.Event()
        self.batch_size = 50
        self._start_worker()
        
    def _start_worker(self):
        """Start the worker thread that processes logs in background"""
        if self.worker_thread is None or not self.worker_thread.is_alive():
            self.stop_event.clear()
            self.worker_thread = threading.Thread(target=self._process_logs)
            self.worker_thread.daemon = True
            self.worker_thread.start()
    
    def emit(self, record):
        """Queue log records to be processed by worker thread"""
        try:
            # Make sure worker is running
            self._start_worker()
            self.log_queue.put(record)
        except Exception as e:
            print(f"Error in log handler: {e}")
    
    def _process_logs(self):
        """Worker thread that processes logs in batches"""
        while not self.stop_event.is_set():
            try:
                records = []
                # Try to get a batch of records without blocking too long
                try:
                    # Get at least one record (blocking)
                    records.append(self.log_queue.get(timeout=1.0))
                    self.log_queue.task_done()
                    
                    # Get more records if available (non-blocking)
                    for _ in range(self.batch_size - 1):
                        if not self.log_queue.empty():
                            records.append(self.log_queue.get_nowait())
                            self.log_queue.task_done()
                        else:
                            break
                except queue.Empty:
                    # No records available, sleep briefly and try again
                    time.sleep(0.1)
                    continue
                
                if records:
                    self._save_records_to_db(records)
            
            except Exception as e:
                print(f"Error in log processing thread: {e}")
                # Sleep briefly to prevent tight error loops
                time.sleep(0.5)
    
    def _save_records_to_db(self, records):
        """Save a batch of records to the database with deduplication"""
        if not records:
            return
            
        conn = None
        retry_count = 0
        max_retries = 3
        
        while retry_count < max_retries:
            try:
                # Create a dedicated connection for logging
                conn = db._create_connection()
                c = conn.cursor()
                
                # Process each record with deduplication
                for record in records:
                    current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')
                    task_id = getattr(record, 'task_id', None)
                    message = record.getMessage()
                    level = record.levelname
                    details = json.dumps(getattr(record, 'details', None)) if hasattr(record, 'details') else None
                    source = record.module
                    
                    # Check for duplicates in the last second
                    c.execute('''
                        SELECT COUNT(*) FROM logs 
                        WHERE message = ? AND level = ? AND task_id = ? 
                        AND timestamp > datetime(?, '-1 seconds')
                    ''', (message, level, task_id, current_time))
                    
                    duplicate_count = c.fetchone()[0]
                    
                    # Only insert if no duplicate found
                    if duplicate_count == 0:
                        c.execute('''
                            INSERT INTO logs (timestamp, level, message, task_id, details, source)
                            VALUES (?, ?, ?, ?, ?, ?)
                        ''', (
                            current_time,
                            level,
                            message,
                            task_id,
                            details,
                            source
                        ))
                        # Commit each record individually to avoid holding locks
                        conn.commit()
                
                # Success, exit the retry loop
                break
                    
            except Exception as e:
                retry_count += 1
                if retry_count >= max_retries:
                    print(f"Error saving log records after {max_retries} attempts: {e}")
                # Sleep briefly before retrying
                time.sleep(0.5 * retry_count)
            finally:
                # Always close the connection in finally block
                if conn:
                    try:
                        conn.close()
                    except:
                        pass
    
    def flush(self):
        """Process remaining log records immediately"""
        # No immediate flush needed as the worker thread handles processing
        pass
    
    def close(self):
        """Clean up resources when handler is closed"""
        self.stop_event.set()
        if self.worker_thread and self.worker_thread.is_alive():
            self.worker_thread.join(timeout=2.0)
        super().close()

# Create handler instance
db_log_handler = DatabaseLogHandler()

def add_log_entry(level, message, task_id=None, details=None, source=None):
    """Add a new log entry to the database with improved error handling"""
    # Direct database insert for reliability
    current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')
    conn = None
    try:
        # Create a dedicated connection for logging
        conn = db._create_connection()
        c = conn.cursor()
        
        # Before inserting, check if a duplicate entry already exists within the last second
        # This helps prevent duplicate logs
        c.execute('''
            SELECT COUNT(*) FROM logs 
            WHERE message = ? AND level = ? AND task_id = ? 
            AND timestamp > datetime(?, '-1 seconds')
        ''', (message, level, task_id, current_time))
        
        duplicate_count = c.fetchone()[0]
        
        # Only insert if no duplicate found
        if duplicate_count == 0:
            c.execute('''
                INSERT INTO logs (timestamp, level, message, task_id, details, source)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (
                current_time,
                level,
                message,
                task_id,
                json.dumps(details) if details else None,
                source or "direct"
            ))
            conn.commit()
        
    except Exception as e:
        print(f"Direct log entry failed: {e}")
    finally:
        # Always close the connection in finally block
        if conn:
            try:
                conn.close()
            except:
                pass

def get_logs(limit=100, level=None, task_id=None, since=None, processing_status=None):
    """Retrieve logs with optional filtering"""
    max_retries = 3
    retry_delay = 0.5
    
    for attempt in range(max_retries):
        conn = None
        try:
            # Use a dedicated connection instead of the pool
            conn = db._create_connection()
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
            
            result = [{
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
            
            return result
            
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e) and attempt < max_retries - 1:
                time.sleep(retry_delay * (2 ** attempt))  # Exponential backoff
                continue
            print(f"Failed to retrieve logs (attempt {attempt+1}): {e}")
            raise
        except Exception as e:
            print(f"Failed to retrieve logs: {e}")
            raise
        finally:
            # Always close the connection in finally block
            if conn:
                try:
                    conn.close()
                except:
                    pass

def clear_old_logs(days=30):
    """Clear logs older than specified days"""
    max_retries = 3
    retry_delay = 0.5
    
    for attempt in range(max_retries):
        conn = None
        try:
            # Use a dedicated connection
            conn = db._create_connection()
            c = conn.cursor()
            c.execute('''
                DELETE FROM logs 
                WHERE timestamp < datetime('now', '-? days')
            ''', (days,))
            conn.commit()
            return c.rowcount
            
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e) and attempt < max_retries - 1:
                time.sleep(retry_delay * (2 ** attempt))  # Exponential backoff
                continue
            print(f"Failed to clear old logs (attempt {attempt+1}): {e}")
            # Try to rollback if possible
            if conn:
                try:
                    conn.execute("ROLLBACK")
                except:
                    pass
            raise
        except Exception as e:
            print(f"Failed to clear old logs: {e}")
            # Try to rollback if possible
            if conn:
                try:
                    conn.execute("ROLLBACK")
                except:
                    pass
            raise
        finally:
            # Always close the connection in finally block
            if conn:
                try:
                    conn.close()
                except:
                    pass