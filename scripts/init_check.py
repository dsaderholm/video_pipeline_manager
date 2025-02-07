import sqlite3
import os
import sys

def get_db_path():
    return os.path.join('webapp', 'database', 'pipeline.db')

# Add parent directory to Python path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def verify_database():
    try:
        # Check if database exists
        db_path = get_db_path()
        if not os.path.exists(db_path):
            print("Database file doesn't exist. It will be created on first run.")
            return True
        
        # Try to connect and verify tables
        with sqlite3.connect(get_db_path()) as conn:
            c = conn.cursor()
            
            # Check tables
            tables = ['video_styles', 'platform_accounts', 'tasks', 'logs']
            for table in tables:
                c.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'")
                if not c.fetchone():
                    print(f"Warning: Table {table} not found!")
                    return False
                print(f"Table {table} verified.")
            
            return True
            
    except Exception as e:
        print(f"Database verification failed: {str(e)}")
        return False

if __name__ == '__main__':
    if verify_database():
        print("Database verification completed successfully.")
    else:
        print("Database verification failed. Please check the error messages above.")