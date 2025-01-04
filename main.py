from app import app, logger

if __name__ == '__main__':
    logger.info("Starting Video Pipeline Manager...")
    try:
        app.run(host='0.0.0.0', port=5000)
    except Exception as e:
        logger.error(f"Failed to start application: {str(e)}")
        raise