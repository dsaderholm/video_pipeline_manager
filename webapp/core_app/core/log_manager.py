# This module has been deprecated as we're using Docker/Portainer for log viewing

# Define dummy variables to avoid import errors in existing code
class DummyLogHandler:
    def __init__(self):
        pass
    
    def emit(self, record):
        pass

db_log_handler = DummyLogHandler()

def get_logs(limit=100, level=None, task_id=None, since=None, processing_status=None):
    """Stub for get_logs"""
    return []

def clear_old_logs(days=30):
    """Stub for clear_old_logs"""
    pass

def add_log_entry(level, message, task_id=None, details=None, source=None, message_hash=None):
    """Stub for add_log_entry"""
    pass