import logging
import sys

def setup_logging(APP_ROOT):
    """Configure basic console logging for docker/portainer visibility"""
    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    
    # Remove existing handlers to prevent duplicates
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # Disable werkzeug access logs
    logging.getLogger('werkzeug').setLevel(logging.WARNING)

    # Console handler for docker/portainer visibility
    console_formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s'
    )
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(console_formatter)
    root_logger.addHandler(console_handler)
    
    # Get the app logger that will be used throughout the application
    app_logger = logging.getLogger('app')
    
    # Allow app_logger to propagate to root logger, which already has the handlers
    # This prevents duplicate logging
    app_logger.propagate = True

    return app_logger