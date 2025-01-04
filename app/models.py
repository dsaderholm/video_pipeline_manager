import sqlite3
import os
import logging
from app import logger

def init_db():
    try:
        if os.path.exists('pipeline.db'):
            os.remove('pipeline.db')
            logger.info("Removed existing database")
            
        with sqlite3.connect('pipeline.db') as conn:
            c = conn.cursor()
            
            # Create generators table
            c.execute('''
                CREATE TABLE IF NOT EXISTS generators (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    generator_curl TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Create utilities table
            c.execute('''
                CREATE TABLE IF NOT EXISTS utilities (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    utility_curl TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Create platform_accounts table with second fallback
            c.execute('''
                CREATE TABLE IF NOT EXISTS platform_accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL,
                    account_name TEXT NOT NULL,
                    uploader_curl TEXT NOT NULL,
                    fallback_curl TEXT,
                    fallback_curl_2 TEXT,
                    default_hashtags TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Create tasks table with retry_count and task_lock table
            c.execute('''
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    generator_id INTEGER,
                    utilities TEXT,
                    schedule TEXT NOT NULL,
                    platforms TEXT NOT NULL,
                    hashtags TEXT,
                    sound_name TEXT,
                    sound_volume TEXT DEFAULT 'background',
                    status TEXT DEFAULT 'pending',
                    email_notify TEXT,
                    retry_count INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(generator_id) REFERENCES generators(id)
                )
            ''')
            
            # Create task_lock table for queue management
            c.execute('''
                CREATE TABLE IF NOT EXISTS task_lock (
                    id INTEGER PRIMARY KEY,
                    locked INTEGER DEFAULT 0,
                    task_id INTEGER,
                    locked_at TIMESTAMP
                )
            ''')
            
            # Initialize task_lock with default record
            c.execute("INSERT INTO task_lock (id, locked) VALUES (1, 0)")
            
            conn.commit()
            logger.info("Database initialized successfully")
            
    except Exception as e:
        logger.error(f"Database initialization error: {str(e)}")
        raise