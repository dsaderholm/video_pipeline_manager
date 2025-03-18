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
    
    # Remove existing handlers to prevent duplicates
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
    
    # Get the app logger that will be used throughout the application
    app_logger = logging.getLogger('app')
    
    # Ensure the app logger isn't propagating to root (which would cause duplication)
    app_logger.propagate = False
    
    # Add the same handlers directly to the app logger
    app_console_handler = logging.StreamHandler(sys.stdout)
    app_console_handler.setLevel(logging.INFO)
    app_console_handler.setFormatter(console_formatter)
    app_logger.addHandler(app_console_handler)
    
    app_file_handler = logging.handlers.RotatingFileHandler(
        os.path.join(logs_dir, 'app.log'),
        maxBytes=1024 * 1024,  # 1MB
        backupCount=5
    )
    app_file_handler.setLevel(logging.DEBUG)
    app_file_handler.setFormatter(detailed_formatter)
    app_logger.addHandler(app_file_handler)

    return app_logger