import sqlite3
import os

def verify_database():
    try:
        # Check if database exists
        if not os.path.exists('pipeline.db'):
            print("Database file doesn't exist. It will be created on first run.")
            return True
        
        # Try to connect and verify tables
        with sqlite3.connect('pipeline.db') as conn:
            c = conn.cursor()
            
            # Check tables
            tables = ['video_styles', 'platform_accounts', 'tasks']
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