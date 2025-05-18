import psycopg2
import os
import sys

# Add parent directory to Python path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from webapp.core_app.core.database import get_db_connection_string

def reset_locks():
    try:
        # Get PostgreSQL connection string
        db_url = get_db_connection_string()
        
        # Connect to PostgreSQL
        with psycopg2.connect(db_url) as conn:
            with conn.cursor() as c:
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