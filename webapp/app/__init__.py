from flask import Flask
from flask_apscheduler import APScheduler
import logging
import logging.handlers
import sys
import os
from datetime import datetime, timedelta
from dotenv import load_dotenv
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED
from apscheduler.triggers.cron import CronTrigger
from app.core.database import db

# Load environment variables
load_dotenv()

# Get absolute path to the application directory
APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def setup_logging():
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
    current_time = datetime.now().timestamp()
    
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
    """Initialize the APScheduler with improved settings and error handling"""
    def handle_job_error(event):
        logger.error(f"Job error occurred. Job ID: {event.job_id}, Error: {event.exception}")
        if event.job_id.startswith('task_'):
            try:
                task_id = int(event.job_id.split('_')[1])
                with db.get_connection() as conn:
                    c = conn.cursor()
                    c.execute("""
                        UPDATE tasks 
                        SET status = 'failed',
                            processing_status = 'failed'
                        WHERE id = ?
                    """, (task_id,))
                    conn.commit()
                    logger.info(f"Updated task {task_id} status to failed after job error")
            except Exception as e:
                logger.error(f"Failed to update task status: {e}")
        
    def handle_job_missed(event):
        logger.warning(f"Job missed! Job ID: {event.job_id}, Scheduled run time: {event.scheduled_run_time}")
        if event.job_id.startswith('task_'):
            try:
                task_id = int(event.job_id.split('_')[1])
                logger.info(f"Rescheduling missed task {task_id}")
            except ValueError:
                pass

    # Initialize scheduler
    scheduler = APScheduler()
    
    # Configure scheduler
    app.config['SCHEDULER_API_ENABLED'] = True
    app.config['SCHEDULER_TIMEZONE'] = os.getenv('TIMEZONE', 'UTC')
    app.config['SCHEDULER_JOB_DEFAULTS'] = {
        'coalesce': True,
        'max_instances': 1,
        'misfire_grace_time': 15  # 15 second grace time
    }
    
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

    # Add night processing job
    from app.core.pipeline import process_night_queue
    scheduler.add_job(
        id='night_processing',
        func=process_night_queue,
        trigger=CronTrigger(minute='*/15'),
        name='Night Video Processing',
        misfire_grace_time=300  # 5 minute grace time
    )

    # Add scheduled uploads job
    from app.core.pipeline import process_scheduled_uploads
    scheduler.add_job(
        id='scheduled_uploads',
        func=process_scheduled_uploads,
        trigger=CronTrigger(minute='*/5'),  # Every 5 minutes
        name='Scheduled Video Uploads',
        misfire_grace_time=240  # 4 minute grace time
    )

    # Log existing tasks
    try:
        with db.get_connection() as conn:
            c = conn.cursor()
            c.execute("""
                SELECT id, name, schedule, processing_status 
                FROM tasks 
                WHERE status != 'completed'
            """)
            tasks = c.fetchall()
            
            logger.info(f"Initializing scheduler with {len(tasks)} active tasks")
            for task in tasks:
                logger.info(f"Task {task[0]} ({task[1]}): Schedule = {task[2]}, Processing = {task[3]}")
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
    
    # Initialize scheduler
    app.scheduler = init_scheduler(app)
    
    # Create required directories
    required_dirs = {
        'previews': os.path.join(APP_ROOT, 'previews'),
        'processed': os.path.join(APP_ROOT, 'processed_videos'),
        'logs': os.path.join(APP_ROOT, 'logs')
    }
    
    for dir_name, dir_path in required_dirs.items():
        if not os.path.exists(dir_path):
            os.makedirs(dir_path)
            logger.info(f"Created {dir_name} directory at: {dir_path}")
    
    with app.app_context():
        # Initialize logging system
        try:
            from app.core.log_manager import db_log_handler
            
            # Add database handler to root logger
            logging.getLogger().addHandler(db_log_handler)
            
            # Prevent Flask logger propagation
            app.logger.propagate = False
            
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

        # Add debug route
        @app.route('/debug')
        def debug():
            """Simple debug endpoint to verify app is running"""
            return {
                "status": "running",
                "debug": app.debug,
                "registered_blueprints": list(app.blueprints.keys())
            }

        logger.info("Application initialization completed successfully")
        return app

# Create the application instance
app = create_app()