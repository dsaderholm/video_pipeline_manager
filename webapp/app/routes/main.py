from flask import render_template, Blueprint
import logging

# Create blueprint and logger
main_bp = Blueprint('main', __name__)
logger = logging.getLogger(__name__)

# Logging to verify route registration
logger.debug("Registering main routes")

@main_bp.route('/')
def index():
    logger.debug("Index route called")
    try:
        return render_template('index.html')
    except Exception as e:
        logger.error(f"Error rendering index: {str(e)}")
        return str(e), 500

@main_bp.route('/manage')
def manage():
    logger.debug("Manage route called")
    try:
        return render_template('manage.html')
    except Exception as e:
        logger.error(f"Error rendering manage: {str(e)}")
        return str(e), 500

@main_bp.route('/logs')
def logs():
    logger.debug("Logs route called")
    try:
        return render_template('logs.html')
    except Exception as e:
        logger.error(f"Error rendering logs: {str(e)}")
        return str(e), 500

logger.debug("Main routes module loaded successfully")