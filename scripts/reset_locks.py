import sqlite3
import os
import sys

def get_db_path():
    return os.path.join('webapp', 'database', 'pipeline.db')

# Add parent directory to Python path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def reset_locks():
    try:
        with sqlite3.connect(get_db_path()) as conn:
            c = conn.cursor()
            c.execute('''
                UPDATE task_lock 
                SET locked = 0, 
                    task_id = NULL, 
                    locked_at = NULL 
                WHERE id = 1
            ''')
            conn.commit()
            print("Successfully reset task locks")
    except Exception as e:
        print(f"Error resetting locks: {str(e)}")

if __name__ == '__main__':
    reset_locks()