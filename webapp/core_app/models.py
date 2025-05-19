import psycopg2
import os
import logging
import time
from webapp.core_app import app_logger as logger
from webapp.core_app.core.database import get_db_connection_string

def init_db():
    try:
        # Connect to PostgreSQL
        max_retries = 10
        retry_delay = 2  # seconds
        db_url = get_db_connection_string()
        
        for attempt in range(max_retries):
            try:
                # Attempt to connect to PostgreSQL
                conn = psycopg2.connect(db_url)
                break  # If successful, break out of the loop
            except psycopg2.OperationalError as e:
                if attempt < max_retries - 1:
                    logger.warning(f"Retrying PostgreSQL connection (attempt {attempt+1}/{max_retries}): {e}")
                    time.sleep(retry_delay)
                else:
                    logger.error(f"Failed to connect to PostgreSQL after {max_retries} attempts: {e}")
                    raise
        
        with conn:
            c = conn.cursor()
            
            # Create generators table
            c.execute('''
                CREATE TABLE IF NOT EXISTS generators (
                    id SERIAL PRIMARY KEY,
                    name TEXT NOT NULL,
                    generator_curl TEXT NOT NULL,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Create utilities table
            c.execute('''
                CREATE TABLE IF NOT EXISTS utilities (
                    id SERIAL PRIMARY KEY,
                    name TEXT NOT NULL,
                    utility_curl TEXT NOT NULL,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Create platforms table
            c.execute('''
                CREATE TABLE IF NOT EXISTS platforms (
                    id SERIAL PRIMARY KEY,
                    name TEXT NOT NULL,
                    uploader_curl TEXT NOT NULL,
                    fallback_curl TEXT,
                    fallback_curl_2 TEXT,
                    default_hashtags TEXT,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # Create tasks table first (since it's referenced by task_platform_accounts)
            c.execute('''
                CREATE TABLE IF NOT EXISTS tasks (
                    id SERIAL PRIMARY KEY,
                    name TEXT NOT NULL,
                    generator_id INTEGER,
                    utilities TEXT,
                    schedule TEXT NOT NULL,
                    hashtags TEXT,
                    sound_name TEXT,
                    sound_volume TEXT DEFAULT 'background',
                    status TEXT DEFAULT 'pending',
                    email_notify TEXT,
                    retry_count INTEGER DEFAULT 0,
                    processing_status TEXT DEFAULT 'pending',
                    processed_video_path TEXT,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(generator_id) REFERENCES generators(id)
                )
            ''')
            
            # Create task_platform_accounts junction table
            c.execute('''
                CREATE TABLE IF NOT EXISTS task_platform_accounts (
                    id SERIAL PRIMARY KEY,
                    task_id INTEGER NOT NULL,
                    platform_id INTEGER NOT NULL,
                    account_name TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE,
                    FOREIGN KEY(platform_id) REFERENCES platforms(id) ON DELETE CASCADE
                )
            ''')

            # Create generated_videos table
            c.execute('''
                CREATE TABLE IF NOT EXISTS generated_videos (
                    id SERIAL PRIMARY KEY,
                    task_id INTEGER NOT NULL,
                    original_name TEXT NOT NULL,
                    processed_path TEXT NOT NULL,
                    scheduled_time TIMESTAMP WITH TIME ZONE NOT NULL,
                    status TEXT DEFAULT 'pending',
                    upload_status TEXT DEFAULT 'pending',
                    error_message TEXT,
                    retry_count INTEGER DEFAULT 0,
                    generated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    uploaded_at TIMESTAMP WITH TIME ZONE,
                    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
                )
            ''')
            
            # Create task_lock table for queue management
            c.execute('''
                CREATE TABLE IF NOT EXISTS task_lock (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    locked INTEGER DEFAULT 0,
                    task_id INTEGER,
                    locked_at TIMESTAMP WITH TIME ZONE
                )
            ''')
            
            # NOTE: Logs table removed - using Portainer for log viewing
            
            # Initialize task_lock with default record if it doesn't exist
            c.execute('''
                INSERT INTO task_lock (id, locked) 
                VALUES (1, 0) 
                ON CONFLICT (id) DO NOTHING
            ''')

            # Note: We don't need migration code from SQLite since we're starting fresh
            # This section would be used if we were migrating data from the old SQLite database
            # Since you mentioned not needing to migrate data, we'll skip this part
            
            # Add table check to see if a column exists in PostgreSQL
            def column_exists(table, column):
                c.execute("""
                    SELECT column_name 
                    FROM information_schema.columns 
                    WHERE table_name = %s AND column_name = %s
                """, (table, column))
                return bool(c.fetchone())

            # Add new columns to tasks table if they don't exist
            if not column_exists('tasks', 'processing_status'):
                # Add processing_status column
                c.execute("ALTER TABLE tasks ADD COLUMN processing_status TEXT DEFAULT 'pending'")
                logger.info("Added processing_status column to tasks table")

            if not column_exists('tasks', 'processed_video_path'):
                # Add processed_video_path column
                c.execute("ALTER TABLE tasks ADD COLUMN processed_video_path TEXT")
                logger.info("Added processed_video_path column to tasks table")
            
            # Check if uploaded_at column exists in generated_videos table
            if not column_exists('generated_videos', 'uploaded_at'):
                # Add uploaded_at column
                try:
                    c.execute("ALTER TABLE generated_videos ADD COLUMN uploaded_at TIMESTAMP WITH TIME ZONE")
                    logger.info("Added uploaded_at column to generated_videos table")
                except Exception as e:
                    logger.error(f"Failed to add uploaded_at column: {str(e)}")
            
            conn.commit()
            logger.info("Database tables verified/initialized successfully")
        conn.close()
            
    except Exception as e:
        logger.error(f"Database initialization error: {str(e)}")
        raise