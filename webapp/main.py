from webapp.core_app import app, app_logger as logger
import os

def setup_app():
    """Initialize the application only once"""
    logger.info("Starting Video Pipeline Manager...")
    
    # Print all registered routes
    logger.info("Registered routes:")
    for rule in app.url_map.iter_rules():
        logger.info(f"Route: {rule}")
    
    return app

if __name__ == '__main__':
    try:
        application = setup_app()
        # Run the app with debug mode from environment or default to False
        debug_mode = os.getenv('FLASK_DEBUG', '0').lower() in ('1', 'true', 'yes')
        application.run(
            host='0.0.0.0', 
            port=int(os.getenv('FLASK_PORT', '5000')),
            debug=debug_mode,
            threaded=True  # Enable threading for better concurrent request handling
        )
    except Exception as e:
        logger.error(f"Failed to start application: {str(e)}")
        raise