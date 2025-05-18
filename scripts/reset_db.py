import os
import sys
import psycopg2

# Add parent directory to Python path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from webapp.core_app.models import init_db
from webapp.core_app.core.database import get_db_connection_string

def reset_database():
    """Drop and recreate the PostgreSQL database"""
    db_url = get_db_connection_string()
    
    # Extract database name from connection string
    db_name = db_url.split('/')[-1]
    connection_params = db_url.rsplit('/', 1)[0] + '/postgres'  # Connect to postgres db instead
    
    print(f"Connecting to PostgreSQL server to reset database: {db_name}")
    
    try:
        # Connect to 'postgres' database to manage other databases
        conn = psycopg2.connect(connection_params)
        conn.autocommit = True  # Required for database drop/create
        cursor = conn.cursor()
        
        # Drop database if it exists
        cursor.execute(f"DROP DATABASE IF EXISTS {db_name}")
        print(f"Dropped database {db_name} if it existed")
        
        # Create database
        cursor.execute(f"CREATE DATABASE {db_name}")
        print(f"Created new empty database {db_name}")
        
        # Close connection
        cursor.close()
        conn.close()
        
        # Initialize tables
        print("Initializing tables...")
        init_db()
        print("Database has been reinitialized with empty tables.")
        
    except Exception as e:
        print(f"Error resetting database: {e}")
        sys.exit(1)

if __name__ == '__main__':
    print("This will completely reset the database. All data will be lost!")
    response = input("Do you want to continue? (y/n): ")
    
    if response.lower() == 'y':
        reset_database()
    else:
        print("Database reset canceled.")