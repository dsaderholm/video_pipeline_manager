from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
import logging
from dotenv import load_dotenv
import os

# Load environment variables from .env file
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
scheduler = BackgroundScheduler()
scheduler.start()

# Initialize log manager
from app.core.log_manager import db_log_handler
db_log_handler.initialize()

# Import routes
from app.routes import generators, utilities, platforms, tasks, logs

# Initialize database
from app.models import init_db
init_db()