import os
import sys
import sqlite3
from datetime import datetime, timedelta

# Add parent directory to Python path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import the necessary modules after setting the path
from webapp.core_app.core.pipeline import process_night_queue, process_scheduled_uploads, check_for_missed_processing

def get_db_path():
    return os.path.join('webapp', 'database', 'pipeline.db')

def reset_locks():
    """Reset any existing locks before processing"""
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

def force_process_all():
    """Force processing of all eligible videos"""
    try:
        print("Forcing missed processing check...")
        check_for_missed_processing(force_process=True)
        
        print("Running night queue processing...")
        process_night_queue()
        
        print("Running scheduled uploads check...")
        process_scheduled_uploads()
        
        print("Processing complete. Check logs for any errors.")
    except Exception as e:
        print(f"Error during forced processing: {str(e)}")

def list_pending_videos():
    """List all pending videos that are waiting for processing or upload"""
    try:
        with sqlite3.connect(get_db_path()) as conn:
            c = conn.cursor()
            
            print("\n=== Pending Videos for Upload ===")
            c.execute('''
                SELECT 
                    v.id, 
                    t.name AS task_name, 
                    v.original_name, 
                    v.scheduled_time,
                    v.upload_status,
                    v.processed_path,
                    v.retry_count
                FROM generated_videos v
                JOIN tasks t ON v.task_id = t.id
                WHERE v.upload_status = 'pending'
                ORDER BY v.scheduled_time ASC
            ''')
            
            videos = c.fetchall()
            if not videos:
                print("No pending videos found.")
            else:
                for video in videos:
                    print(f"ID: {video[0]}")
                    print(f"Task: {video[1]}")
                    print(f"Video: {video[2]}")
                    print(f"Scheduled: {video[3]}")
                    print(f"Status: {video[4]}")
                    print(f"Path: {video[5]}")
                    print(f"Retry count: {video[6]}")
                    print("-----------------------------")
            
            print("\n=== Tasks Scheduled for Tomorrow ===")
            tomorrow = datetime.now() + timedelta(days=1)
            tomorrow_day = tomorrow.strftime('%A')[:3].lower()
            
            c.execute('''
                SELECT id, name, schedule, status, processing_status
                FROM tasks
                WHERE schedule LIKE ?
                AND status != 'failed'
            ''', (f'%{tomorrow_day}|%',))
            
            tasks = c.fetchall()
            if not tasks:
                print(f"No tasks scheduled for tomorrow ({tomorrow_day}).")
            else:
                for task in tasks:
                    print(f"ID: {task[0]}")
                    print(f"Name: {task[1]}")
                    print(f"Schedule: {task[2]}")
                    print(f"Status: {task[3]}")
                    print(f"Processing: {task[4]}")
                    print("-----------------------------")
            
    except Exception as e:
        print(f"Error listing pending videos: {str(e)}")

if __name__ == '__main__':
    print("=== Video Pipeline Force Processing Tool ===")
    print("This script will force processing of all eligible videos")
    print("1. Reset locks")
    print("2. Check for missed processing")
    print("3. Run night queue")
    print("4. Process scheduled uploads")
    print()
    
    # First list pending videos
    list_pending_videos()
    
    # Then ask for confirmation
    confirm = input("\nDo you want to proceed with force processing? (y/n): ")
    if confirm.lower() == 'y':
        reset_locks()
        force_process_all()
    else:
        print("Aborted. No changes made.")
