from flask import Flask
from flask_apscheduler import APScheduler
import logging
import logging.handlers
import sys
import os
from datetime import datetime, timedelta
from dotenv import load_dotenv
import sqlite3

# Set up logging first, before any other imports
APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Create a global scheduler instance and initialization flag
scheduler = APScheduler()
_scheduler_initialized = False

def setup_logging():
    """Configure basic console logging for docker visibility"""
    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    
    # Remove existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # Disable werkzeug access logs
    logging.getLogger('werkzeug').setLevel(logging.WARNING)

    # Console handler for docker visibility
    console_formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s'
    )
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(console_formatter)
    root_logger.addHandler(console_handler)

    return logging.getLogger('app')

# Initialize logger immediately
app_logger = setup_logging()
logger = app_logger

# Now do the rest of the imports
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED
from apscheduler.triggers.cron import CronTrigger
from apscheduler.jobstores.base import JobLookupError
from webapp.core_app.core.database import db  # Updated import path

load_dotenv()

def cleanup_preview_files():
    """Clean up preview files older than 1 hour"""
    preview_dir = os.path.join(APP_ROOT, 'previews')
    if not os.path.exists(preview_dir):
        return
        
    app_logger.info("Starting scheduled preview files cleanup")
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
        app_logger.info(f"Preview cleanup complete. Deleted: {deleted_count}, Errors: {error_count}")

from webapp.core_app.core.pipeline import process_night_queue, process_scheduled_uploads  # Updated import path

def cleanup_all_processed_videos():
    """Clean up all processed videos"""
    processed_dir = os.path.join(APP_ROOT, 'processed_videos')
    if not os.path.exists(processed_dir):
        return
        
    app_logger.info("Starting processed videos cleanup")
    deleted_count = 0
    error_count = 0
    platform_suffixes = ['_YouTube', '_Instagram', '_TikTok', '_Twitter', '_Facebook']
    
    # Get list of all video files to delete
    video_files = []
    for file in os.listdir(processed_dir):
        if not file.endswith('.mp4'):
            continue
            
        file_path = os.path.join(processed_dir, file)
        video_files.append(file_path)
        
    # Add any platform-specific videos that might have been missed
    for file_path in list(video_files):  # Use list() to create a copy so we can modify video_files
        file_name = os.path.basename(file_path)
        name_base, ext = os.path.splitext(file_name)
        
        # Check if this is already a platform-specific file
        is_platform_file = any(name_base.endswith(suffix) for suffix in platform_suffixes)
        
        # If not a platform file, look for corresponding platform files
        if not is_platform_file:
            for suffix in platform_suffixes:
                platform_file = os.path.join(processed_dir, f"{name_base}{suffix}{ext}")
                if os.path.exists(platform_file) and platform_file not in video_files:
                    video_files.append(platform_file)
    
    # Delete all identified video files
    for file_path in video_files:
        try:
            os.remove(file_path)
            deleted_count += 1
            logger.debug(f"Deleted processed video: {os.path.basename(file_path)}")
        except Exception as e:
            error_count += 1
            logger.error(f"Error cleaning up processed video {file_path}: {e}")
    
    # Look for any "safe" temporary files that might have been missed
    temp_files = [f for f in os.listdir(processed_dir) if f.startswith('upload_') and f.endswith('.mp4')]
    for temp_file in temp_files:
        temp_path = os.path.join(processed_dir, temp_file)
        try:
            os.remove(temp_path)
            deleted_count += 1
            logger.debug(f"Deleted temporary video: {temp_file}")
        except Exception as e:
            error_count += 1
            logger.error(f"Error cleaning up temporary video {temp_path}: {e}")
    
    if deleted_count > 0 or error_count > 0:
        app_logger.info(f"Processed video cleanup complete. Deleted: {deleted_count}, Errors: {error_count}")

def init_scheduler(app):
    """Initialize the APScheduler with improved settings and error handling"""
    global _scheduler_initialized
    
    # Only initialize once
    if _scheduler_initialized:
        logger.debug("Scheduler already initialized, skipping...")
        return scheduler

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
                    app_logger.info(f"Updated task {task_id} status to failed after job error")
            except Exception as e:
                logger.error(f"Failed to update task status: {e}")
        
    def handle_job_missed(event):
        logger.warning(f"Job missed! Job ID: {event.job_id}, Scheduled run time: {event.scheduled_run_time}")
        if event.job_id.startswith('task_'):
            try:
                task_id = int(event.job_id.split('_')[1])
                app_logger.info(f"Rescheduling missed task {task_id}")
            except ValueError:
                pass

    # Configure scheduler
    app.config['SCHEDULER_API_ENABLED'] = True
    app.config['SCHEDULER_TIMEZONE'] = os.getenv('TIMEZONE', 'UTC')
    app.config['SCHEDULER_JOB_DEFAULTS'] = {
        'coalesce': True,
        'max_instances': 1,
        'misfire_grace_time': 60  # Increased to 60 seconds
    }
    
    # Initialize the scheduler if it hasn't been started
    if not scheduler.running:
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

        # Add night processing job - run at staggered times (2, 17, 32, 47) to avoid lock contention
        scheduler.add_job(
            id='night_processing',
            func=process_night_queue,
            trigger=CronTrigger(minute='2,17,32,47'),  # Staggered to avoid lock collisions
            name='Night Video Processing',
            misfire_grace_time=900,  # 15 minute grace time
            max_instances=1,
            replace_existing=True,
            coalesce=True
        )
        
        # Add an additional dedicated night processing job at the exact start time
        night_hour, night_minute = os.getenv('NIGHT_PROCESSING_START', '01:30').split(':')
        scheduler.add_job(
            id='dedicated_night_processing',
            func=process_night_queue,
            trigger=CronTrigger(hour=int(night_hour), minute=int(night_minute)),
            name='Dedicated Night Processing',
            misfire_grace_time=3600,  # 1 hour grace time
            max_instances=1,
            replace_existing=True,
            coalesce=True
        )
        
        # Add a job to run 17 minutes after the start time to ensure processing happens and avoid lock contention
        next_minute = int(night_minute) + 17  # Changed from 15 to 17 to avoid lock collision
        next_hour = int(night_hour)
        
        # Handle minute overflow
        if next_minute >= 60:
            next_minute = next_minute % 60
            next_hour = (next_hour + 1) % 24
            
        scheduler.add_job(
            id='night_processing_followup',
            func=process_night_queue,
            trigger=CronTrigger(hour=next_hour, minute=next_minute),
            name='Night Processing Followup',
            misfire_grace_time=3600,  # 1 hour grace time
            max_instances=1,
            replace_existing=True,
            coalesce=True
        )

        # Add scheduled uploads job - run at 3, 8, 13, etc. minutes to avoid lock contention with night processing
        scheduler.add_job(
            id='scheduled_uploads',
            func=process_scheduled_uploads,
            trigger=CronTrigger(minute='3,8,13,18,23,28,33,38,43,48,53,58'),  # Offset by 3 minutes 
            name='Scheduled Video Uploads',
            misfire_grace_time=240,  # 4 minute grace time
            max_instances=1,
            replace_existing=True,
            coalesce=True
        )

        # Add processed videos cleanup job
        scheduler.add_job(
            id='cleanup_processed',
            func=cleanup_all_processed_videos,
            trigger='interval',
            hours=24,  # Run daily
            next_run_time=datetime.now() + timedelta(minutes=10)
        )
        
        # Add scheduled database maintenance job
        scheduler.add_job(
            id='database_maintenance',
            func=db.checkpoint_wal,  # Force WAL checkpoint
            trigger='interval',
            hours=4,  # Run every 4 hours
            name='Database Maintenance',
            misfire_grace_time=3600,  # 1 hour grace time 
            max_instances=1,
            replace_existing=True
        )
        logger.info("Scheduled database maintenance job")
        
        # Add automatic lock reset job to clear any stuck locks
        scheduler.add_job(
            id='auto_lock_reset',
            func='webapp.core_app.core.pipeline:force_release_lock',
            trigger='interval',
            hours=1,  # Run every hour
            name='Automatic Lock Reset',
            misfire_grace_time=1800,  # 30 minute grace time
            max_instances=1,
            replace_existing=True
        )
        logger.info("Scheduled automatic lock reset job")

        # Rebuild schedules for existing tasks
        try:
            with db.get_connection() as conn:
                c = conn.cursor()
                try:
                    c.execute("""
                        SELECT id, name, schedule, processing_status 
                        FROM tasks 
                        WHERE status != 'completed'
                    """)
                    tasks = c.fetchall()
                    
                    logger.info(f"Initializing scheduler with {len(tasks)} active tasks")
                    for task in tasks:
                        task_id = task[0]
                        task_name = task[1]
                        task_schedule = task[2]
                        processing_status = task[3]
                        
                        task_dict = {
                            'id': task_id,
                            'name': task_name,
                            'schedule': task_schedule,
                            'processing_status': processing_status
                        }
                        logger.info(f"Task {task_dict['id']} ({task_dict['name']}): Schedule = {task_dict['schedule']}, Processing = {task_dict['processing_status']}")
                        
                        # Add night processing job for each task
                        night_job_id = f'task_{task_id}_night_processing'
                        night_hour, night_minute = os.getenv('NIGHT_PROCESSING_START', '22:00').split(':')
                        
                        # Replace the job if it exists
                        try:
                            # First try to remove any existing job
                            try:
                                scheduler.remove_job(night_job_id)
                            except JobLookupError:
                                pass  # Job doesn't exist yet
                            try:
                                scheduler.add_job(
                                    func='webapp.core_app.core.pipeline:process_night_queue',
                                    trigger='cron',
                                    hour=int(night_hour),
                                    minute=int(night_minute),
                                    id=night_job_id,
                                    misfire_grace_time=3600,  # 1 hour grace time
                                    replace_existing=True
                                )
                                logger.info(f"Scheduled night processing for task {task_id} at {night_hour}:{night_minute}")
                            except Exception as e:
                                logger.error(f"Failed to schedule night processing for task {task_id}: {e}")
                        except Exception as e:
                            logger.error(f"Error scheduling night processing for task {task_id}: {e}")
                        
                        # Parse schedule for uploads and recreate upload jobs
                        day_schedules = task_schedule.split(';')
                        for day_schedule in day_schedules:
                            if not day_schedule.strip():
                                continue
                                
                            day, times = day_schedule.split('|')
                            day_number = {
                                'sun': 0, 'mon': 1, 'tue': 2, 'wed': 3, 
                                'thu': 4, 'fri': 5, 'sat': 6
                            }[day.lower()]
                            
                            # Schedule each upload time slot
                            for time_str in times.split(','):
                                hour, minute = map(int, time_str.strip().split(':'))
                                
                                # Schedule the upload job
                                upload_job_id = f'task_{task_id}_{day}_{hour}_{minute}'
                                
                                # Replace the job if it exists
                                try:
                                    # First try to remove any existing job
                                    try:
                                        scheduler.remove_job(upload_job_id)
                                    except JobLookupError:
                                        pass  # Job doesn't exist yet
                                    try:
                                        from webapp.core_app.core.pipeline import process_video_upload
                                        scheduler.add_job(
                                            func=process_video_upload,
                                            trigger='cron',
                                            day_of_week=day_number,
                                            hour=hour,
                                            minute=minute,
                                            args=[task_id],
                                            id=upload_job_id,
                                            misfire_grace_time=300,  # 5 minutes grace time
                                            replace_existing=True,  # Ensure it replaces any existing job with same ID
                                            coalesce=True
                                        )
                                        logger.info(f"Scheduled upload for task {task_id} on {day} at {hour:02d}:{minute:02d}")
                                    except Exception as e:
                                        logger.error(f"Failed to schedule upload for task {task_id} on {day} at {hour}:{minute}: {e}")
                                except Exception as e:
                                    logger.error(f"Error scheduling upload for task {task_id} on {day} at {hour}:{minute}: {e}")
                    
                except sqlite3.OperationalError as e:
                    logger.warning(f"Could not load existing tasks: {e}")
        except Exception as e:
            logger.error(f"Failed to load tasks: {e}")

        scheduler.start()
        _scheduler_initialized = True
        logger.info("Scheduler initialized successfully")
        
        # Add startup check for missed night processing
        if not scheduler.running:
            logger.error("Scheduler not running, skipping missed processing check")
        else:
            # Add a job to check for missed processing on startup with a slight delay
            scheduler.add_job(
                id='startup_missed_processing_check',
                func='webapp.core_app.core.pipeline:check_for_missed_processing',
                trigger='date',
                run_date=datetime.now() + timedelta(seconds=60),  # Increased to 60 seconds delay
                args=[True],  # Force process
                misfire_grace_time=3600  # 1 hour grace time
            )
            logger.info("Scheduled missed processing check for startup with 60 second delay")

    return scheduler

def create_app():
    """Create and configure the Flask application"""
    app_logger.info("Starting application initialization...")

    # Create Flask app
    app = Flask(__name__,
                template_folder=os.path.join(APP_ROOT, 'templates'),
                static_folder=os.path.join(APP_ROOT, 'static'))
    
    # Create required directories
    required_dirs = {
    'previews': os.path.join(APP_ROOT, 'previews'),
    'processed': os.path.join(APP_ROOT, 'processed_videos'),
    'backup_logs': os.path.join(APP_ROOT, 'backup_logs')
    }
    
    for dir_name, dir_path in required_dirs.items():
        if not os.path.exists(dir_path):
            os.makedirs(dir_path)
            app_logger.info(f"Created {dir_name} directory at: {dir_path}")
    
    with app.app_context():
        # Initialize database FIRST
        try:
            from webapp.core_app.models import init_db  # Updated import path
            init_db()
            app_logger.info("Main database initialized successfully")
            
            # Database configuration
            from webapp.core_app.core.database import db
            # No need to fix logs table anymore
        except Exception as e:
            logger.error(f"Failed to initialize main database: {str(e)}")
            raise

        # Ensure we're only using console logging for Docker
        # Prevent Flask logger propagation
        app.logger.propagate = False
        app_logger.info("Using console logging only (viewable in Docker logs)")

        # Initialize scheduler THIRD
        app.scheduler = init_scheduler(app)

        # Register blueprints FOURTH
        try:
            blueprints = [
                ('webapp.core_app.routes.main', 'main_bp'),  # Updated import paths
                ('webapp.core_app.routes.generators', 'generators_bp'),
                ('webapp.core_app.routes.utilities', 'utilities_bp'),
                ('webapp.core_app.routes.platforms', 'platforms_bp'),
                ('webapp.core_app.routes.tasks', 'tasks_bp')
            ]
            
            for module_path, blueprint_name in blueprints:
                try:
                    module = __import__(module_path, fromlist=[blueprint_name])
                    blueprint = getattr(module, blueprint_name)
                    app.register_blueprint(blueprint)
                    app_logger.info(f"Registered blueprint: {blueprint_name}")
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

        app_logger.info("Application initialization completed successfully")
        return app

# Create the application instance
app = create_app()