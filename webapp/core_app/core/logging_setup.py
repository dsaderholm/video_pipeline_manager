import logging
import logging.handlers
import os
import sys

def setup_logging(APP_ROOT):
    """Configure the logging system with file and console handlers"""
    # Create logs directory if it doesn't exist
    logs_dir = os.path.join(APP_ROOT, 'logs')
    if not os.path.exists(logs_dir):
        os.makedirs(logs_dir)

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    
    # Remove existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # Disable werkzeug access logs
    logging.getLogger('werkzeug').setLevel(logging.WARNING)

    # Create formatters
    detailed_formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    console_formatter = logging.Formatter(
        '%(levelname)s - %(message)s'
    )

    # Console handler (less verbose)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(console_formatter)
    root_logger.addHandler(console_handler)

    # File handler (detailed)
    file_handler = logging.handlers.RotatingFileHandler(
        os.path.join(logs_dir, 'app.log'),
        maxBytes=1024 * 1024,  # 1MB
        backupCount=5
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(detailed_formatter)
    root_logger.addHandler(file_handler)

    return logging.getLogger('app')