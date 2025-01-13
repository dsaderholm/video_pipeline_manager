from flask import Flask
from flask_apscheduler import APScheduler
import logging
import logging.handlers
import sys
from dotenv import load_dotenv
import os
import sqlite3
import time
from datetime import datetime, timedelta
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED

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

    return logging.getLogger(__name__)

# Initialize logger
logger = setup_logging()

def cleanup_preview_files():
    """Clean up preview files older than 1 hour"""
    preview_dir = os.path.join(APP_ROOT, 'previews')
    if not os.path.exists(preview_dir):
        return
        
    logger.info("Starting scheduled preview files cleanup")
    deleted_count = 0
    error_count = 0
    current_time = time.time()
    
    for file in os.listdir(preview_dir):
        if not file.startswith('preview_task_'):
            continue
            
        file_path = os.path.join(preview_dir, file)
        # If file is older than 1 hour
        if os.path.getmtime(file_path) < current_time - 3600:
            try:
                os.remove(file_path)
                deleted_count += 1
                logger.debug(f"Deleted old preview file: {file}")
            except Exception as e:
                error_count += 1
                logger.error(f"Error cleaning up old preview file {file_path}: {e}")
    
    if deleted_count > 0 or error_count > 0:
        logger.info(f"Preview cleanup complete. Deleted: {deleted_count}, Errors: {error_count}")

def init_scheduler(app):
    """Initialize the APScheduler with improved logging and error handling"""
    def handle_job_error(event):
        logger.error(f"Job error occurred. Job ID: {event.job_id}, Error: {event.exception}")
        if event.job_id.startswith('task_'):
            try:
                task_id = int(event.job_id.split('_')[1])
                with sqlite3.connect('pipeline.db') as conn:
                    c = conn.cursor()
                    c.execute("UPDATE tasks SET status = 'failed' WHERE id = ?", (task_id,))
                    conn.commit()
            except Exception as e:
                logger.error(f"Failed to update task status: {e}")
        
    def handle_job_missed(event):
        logger.warning(f"Job missed! Job ID: {event.job_id}, Scheduled run time: {event.scheduled_run_time}")

    # Set up the scheduler with configuration
    scheduler = APScheduler()
    app.config['SCHEDULER_API_ENABLED'] = True
    app.config['SCHEDULER_TIMEZONE'] = os.getenv('TIMEZONE', 'UTC')
    scheduler.init_app(app)

    # Add event listeners
    scheduler.scheduler.add_listener(handle_job_error, EVENT_JOB_ERROR)
    scheduler.scheduler.add_listener(handle_job_missed, EVENT_JOB_MISSED)

    # Add cleanup job
    scheduler.add_job(
        id='cleanup_previews',
        func=cleanup_preview_files,
        trigger='interval',
        hours=1,
        next_run_time=datetime.now() + timedelta(minutes=1)
    )

    # Log all existing tasks
    try:
        with sqlite3.connect('pipeline.db') as conn:
            c = conn.cursor()
            c.execute("SELECT id, name, schedule FROM tasks WHERE status != 'completed'")
            tasks = c.fetchall()
            
            logger.info(f"Initializing scheduler with {len(tasks)} active tasks")
            for task in tasks:
                logger.info(f"Task {task[0]} ({task[1]}): Schedule = {task[2]}")
    except Exception as e:
        logger.error(f"Failed to log existing tasks: {e}")

    scheduler.start()
    return scheduler

def create_app():
    """Create and configure the Flask application"""
    logger.info("Starting application initialization...")

    # Create Flask app
    app = Flask(__name__,
                template_folder=os.path.join(APP_ROOT, 'templates'),
                static_folder=os.path.join(APP_ROOT, 'static'))
    
    # Initialize scheduler with improved error handling and logging
    app.scheduler = init_scheduler(app)
    
    # Create previews directory if it doesn't exist
    previews_dir = os.path.join(APP_ROOT, 'previews')
    if not os.path.exists(previews_dir):
        os.makedirs(previews_dir)
        logger.info("Created previews directory")
    
    with app.app_context():
        # Initialize log manager
        try:
            from app.core.log_manager import db_log_handler, init_logs
            
            # Initialize logs table
            init_logs()
            
            # Add database handler to both root logger and Flask logger
            logging.getLogger().addHandler(db_log_handler)
            app.logger.propagate = False
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