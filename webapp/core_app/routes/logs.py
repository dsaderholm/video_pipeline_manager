# Logs routes removed as we're using Portainer for log viewing

from flask import Blueprint

# Create empty blueprint to avoid import errors
logs_bp = Blueprint('logs', __name__, url_prefix='')

# No routes defined - using Portainer for log viewing instead
