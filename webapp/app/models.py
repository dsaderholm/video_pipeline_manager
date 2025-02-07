import sqlite3
import os
import logging
from app import logger

def init_db():
    try:
        db_path = os.path.join('webapp', 'database', 'pipeline.db')
        with sqlite3.connect(db_path) as conn:
            c = conn.cursor()
            
            # Create generators table
            c.execute('''
                CREATE TABLE IF NOT EXISTS generators (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    generator_curl TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Create utilities table
            c.execute('''
                CREATE TABLE IF NOT EXISTS utilities (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    utility_curl TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Create platforms table
            c.execute('''
                CREATE TABLE IF NOT EXISTS platforms (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    uploader_curl TEXT NOT NULL,
                    fallback_curl TEXT,
                    fallback_curl_2 TEXT,
                    default_hashtags TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # Create task_platform_accounts junction table
            c.execute('''
                CREATE TABLE IF NOT EXISTS task_platform_accounts (
                    id INTEGER PRIMARY KEY,
                    task_id INTEGER NOT NULL,
                    platform_id INTEGER NOT NULL,
                    account_name TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE,
                    FOREIGN KEY(platform_id) REFERENCES platforms(id) ON DELETE CASCADE
                )
            ''')
            
            # Create tasks table
            c.execute('''
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY,
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
            
            # Initialize task_lock with default record if it doesn't exist
            c.execute("INSERT OR IGNORE INTO task_lock (id, locked) VALUES (1, 0)")

            # Handle migration of existing data
            try:
                # Check if old platform_accounts table exists
                c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='platform_accounts'")
                if c.fetchone():
                    # Migrate data from platform_accounts to platforms
                    c.execute('''
                        INSERT INTO platforms (name, uploader_curl, fallback_curl, fallback_curl_2, default_hashtags)
                        SELECT DISTINCT platform, uploader_curl, fallback_curl, fallback_curl_2, default_hashtags
                        FROM platform_accounts
                    ''')
                    
                    # Get all tasks and their platforms
                    c.execute("SELECT id, platforms FROM tasks WHERE platforms IS NOT NULL")
                    tasks = c.fetchall()
                    
                    # For each task, create entries in task_platform_accounts
                    for task_id, platforms_str in tasks:
                        if platforms_str:
                            platform_ids = platforms_str.split(',')
                            for platform_id in platform_ids:
                                # Get the account name from the old platform_accounts table
                                c.execute("""
                                    SELECT account_name FROM platform_accounts 
                                    WHERE id = ?
                                """, (int(platform_id),))
                                result = c.fetchone()
                                if result:
                                    account_name = result[0]
                                    # Create new task_platform_accounts entry
                                    c.execute("""
                                        INSERT INTO task_platform_accounts 
                                        (task_id, platform_id, account_name)
                                        VALUES (?, ?, ?)
                                    """, (task_id, int(platform_id), account_name))
                    
                    # Drop the old platform_accounts table
                    c.execute("DROP TABLE platform_accounts")
                    
                    # Remove the platforms column from tasks table
                    c.execute('''
                        CREATE TABLE tasks_new (
                            id INTEGER PRIMARY KEY,
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
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            FOREIGN KEY(generator_id) REFERENCES generators(id)
                        )
                    ''')
                    c.execute('''
                        INSERT INTO tasks_new 
                        SELECT id, name, generator_id, utilities, schedule, 
                               hashtags, sound_name, sound_volume, status, 
                               email_notify, retry_count, created_at 
                        FROM tasks
                    ''')
                    c.execute('DROP TABLE tasks')
                    c.execute('ALTER TABLE tasks_new RENAME TO tasks')

            except Exception as e:
                logger.error(f"Migration error: {str(e)}")
                # Continue with initialization even if migration fails
            
            conn.commit()
            logger.info("Database tables verified/initialized successfully")
            
    except Exception as e:
        logger.error(f"Database initialization error: {str(e)}")
        raise