from flask import Flask
from flask_apscheduler import APScheduler
import logging
import sys
from dotenv import load_dotenv
import os

# Load environment variables from .env file
load_dotenv()

# Get absolute path to the application directory
APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Configure more verbose logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

def create_app():
    # Debug print to verify paths
    logger.debug(f"APP_ROOT: {APP_ROOT}")
    logger.debug(f"Templates folder: {os.path.join(APP_ROOT, 'templates')}")
    logger.debug(f"Static folder: {os.path.join(APP_ROOT, 'static')}")

    app = Flask(__name__,
               template_folder=os.path.join(APP_ROOT, 'templates'),
               static_folder=os.path.join(APP_ROOT, 'static'))

    # Configure scheduler
    app.config['SCHEDULER_API_ENABLED'] = True
    app.config['SCHEDULER_TIMEZONE'] = "UTC"
    scheduler = APScheduler()
    scheduler.init_app(app)
    scheduler.start()
    
    # Make scheduler accessible throughout the app
    app.scheduler = scheduler

    with app.app_context():
        # Initialize log manager
        try:
            from app.core.log_manager import db_log_handler
            db_log_handler.initialize()
        except Exception as e:
            logger.error(f"Failed to initialize db_log_handler: {e}")

        # Initialize database
        try:
            from app.models import init_db
            init_db()
            logger.debug("Database initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize database: {e}")

        # Register all blueprints
        try:
            # Import and register main blueprint
            from app.routes.main import main_bp
            app.register_blueprint(main_bp)

            # Import and register generators blueprint
            from app.routes.generators import generators_bp
            app.register_blueprint(generators_bp)

            # Import and register utilities blueprint
            from app.routes.utilities import utilities_bp
            app.register_blueprint(utilities_bp)

            # Import and register platforms blueprint
            from app.routes.platforms import platforms_bp
            app.register_blueprint(platforms_bp)

            # Import and register tasks blueprint
            from app.routes.tasks import tasks_bp
            app.register_blueprint(tasks_bp)

            # Import and register logs blueprint
            from app.routes.logs import logs_bp
            app.register_blueprint(logs_bp)

            # Verify route registration
            logger.debug("Printing all routes:")
            for rule in app.url_map.iter_rules():
                logger.debug(f"Route: {rule.endpoint} - Methods: {rule.methods}")
        except Exception as e:
            logger.error(f"Failed to register blueprints: {e}")
            raise

        # Debug route
        @app.route('/debug')
        def debug():
            return "Debug route working!", 200

        return app

# Create the application instance
app = create_app()