from flask import Flask
from flask_apscheduler import APScheduler
import logging
import logging.handlers
import sys
from dotenv import load_dotenv
import os
import sqlite3

# Load environment variables from .env file
load_dotenv()

# Get absolute path to the application directory
APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def setup_logging():
    """Configure the logging system with both file and database handlers"""
    # Create logs directory if it doesn't exist
    logs_dir = os.path.join(APP_ROOT, 'logs')
    if not os.path.exists(logs_dir):
        os.makedirs(logs_dir)

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

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

    return logging.getLogger(__name__)

# Initialize logger
logger = setup_logging()

def create_app():
    """Create and configure the Flask application"""
    logger.info("Starting application initialization...")

    # Create Flask app
    app = Flask(__name__,
                template_folder=os.path.join(APP_ROOT, 'templates'),
                static_folder=os.path.join(APP_ROOT, 'static'))

    # Configure scheduler
    app.config['SCHEDULER_API_ENABLED'] = True
    app.config['SCHEDULER_TIMEZONE'] = "UTC"
    scheduler = APScheduler()
    scheduler.init_app(app)
    scheduler.start()  # Start scheduler here, before app context
    app.scheduler = scheduler  # Make scheduler accessible throughout the app
    
    with app.app_context():
        # Initialize log manager
        try:
            from app.core.log_manager import db_log_handler, init_logs
            
            # Initialize logs table
            init_logs()
            
            # Add database handler to both root logger and Flask logger
            logging.getLogger().addHandler(db_log_handler)
            app.logger.addHandler(db_log_handler)
            
            logger.info("Database logging system initialized successfully")
            
        except Exception as e:
            logger.error(f"Failed to initialize logging system: {str(e)}")
            raise

        # Initialize database
        try:
            from app.models import init_db
            init_db()
            logger.info("Main database initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize main database: {str(e)}")
            raise

        # Register blueprints
        try:
            blueprints = [
                ('app.routes.main', 'main_bp'),
                ('app.routes.generators', 'generators_bp'),
                ('app.routes.utilities', 'utilities_bp'),
                ('app.routes.platforms', 'platforms_bp'),
                ('app.routes.tasks', 'tasks_bp'),
                ('app.routes.logs', 'logs_bp')
            ]
            
            for module_path, blueprint_name in blueprints:
                try:
                    module = __import__(module_path, fromlist=[blueprint_name])
                    blueprint = getattr(module, blueprint_name)
                    app.register_blueprint(blueprint)
                    logger.info(f"Registered blueprint: {blueprint_name}")
                except Exception as e:
                    logger.error(f"Failed to register blueprint {blueprint_name}: {str(e)}")
                    raise

        except Exception as e:
            logger.error(f"Failed to register blueprints: {str(e)}")
            raise

        # Create debug route
        @app.route('/debug')
        def debug():
            """Simple debug endpoint to verify app is running"""
            return {
                "status": "running",
                "debug": app.debug,
                "registered_blueprints": list(app.blueprints.keys())
            }

        # Log successful initialization
        logger.info("Application initialization completed successfully")
        
        # Add test log entry to verify logging system
        app.logger.info("Test log entry - If you see this in the logs, the logging system is working!")

        return app

# Create the application instance
app = create_app()