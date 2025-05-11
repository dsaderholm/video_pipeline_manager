# This module simplifies logging to work only with Docker logs
import logging

# Create a pass-through handler that doesn't actually do anything
class DockerLogHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.level = logging.NOTSET
    
    def emit(self, record):
        # Do nothing - we want logs to just go to stdout/stderr
        pass
        
    def handle(self, record):
        # Always return False to prevent handling
        return False

# Create a global instance
db_log_handler = DockerLogHandler()

# These functions are just stubs to prevent errors if called
def get_logs(limit=100, level=None, task_id=None, since=None, processing_status=None):
    return []

def clear_old_logs(days=30):
    pass

def add_log_entry(level, message, task_id=None, details=None, source=None, message_hash=None):
    # Just log to the console with task_id if provided
    logger = logging.getLogger('app')
    if task_id:
        message = f"[Task {task_id}] {message}"
    
    # Map level strings to logging levels
    level_map = {
        'DEBUG': logging.DEBUG,
        'INFO': logging.INFO,
        'WARNING': logging.WARNING,
        'ERROR': logging.ERROR,
        'CRITICAL': logging.CRITICAL
    }
    
    # Get numeric level or default to INFO
    log_level = level_map.get(level.upper() if isinstance(level, str) else level, logging.INFO)
    
    # Log directly to the console
    logger.log(log_level, message)