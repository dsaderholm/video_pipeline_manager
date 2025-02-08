from webapp.app import app, logger

if __name__ == '__main__':
    logger.info("Starting Video Pipeline Manager...")
    try:
        # Print all registered routes
        logger.info("Registered routes:")
        for rule in app.url_map.iter_rules():
            logger.info(f"Route: {rule}")
        
        # Run the app with debug mode on
        app.run(host='0.0.0.0', port=5000, debug=False)
    except Exception as e:
        logger.error(f"Failed to start application: {str(e)}")
        raise