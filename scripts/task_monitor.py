import os
import sys
import sqlite3
import json
from datetime import datetime, timedelta

# Add parent directory to Python path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def get_db_path():
    return os.path.join('webapp', 'database', 'pipeline.db')

def print_header(title):
    """Print a formatted header"""
    print("\n" + "=" * 80)
    print(f" {title} ".center(80, "="))
    print("=" * 80)

def check_task_locks():
    """Check if any tasks are currently locked"""
    print_header("Task Lock Status")
    
    try:
        with sqlite3.connect(get_db_path()) as conn:
            c = conn.cursor()
            c.execute("""
                SELECT locked, task_id, locked_at
                FROM task_lock
                WHERE id = 1
            """)
            lock_data = c.fetchone()
            
            if not lock_data:
                print("No lock record found in database!")
                return
            
            locked, task_id, locked_at = lock_data
            
            if locked == 1:
                print(f"ACTIVE LOCK: Task ID {task_id}, Locked at {locked_at}")
                
                # Check if lock is stale
                if locked_at:
                    lock_time = datetime.fromisoformat(locked_at) if "T" in locked_at else datetime.strptime(locked_at, "%Y-%m-%d %H:%M:%S")
                    time_diff = datetime.now() - lock_time
                    if time_diff > timedelta(minutes=10):
                        print(f"WARNING: Lock appears stale! Locked for {time_diff.total_seconds() / 60:.1f} minutes")
                        print("You may want to run reset_locks.py to clear this lock")
                
                if task_id:
                    # Get task info
                    c.execute("SELECT name FROM tasks WHERE id = ?", (task_id,))
                    task = c.fetchone()
                    if task:
                        print(f"Task Name: {task[0]}")
            else:
                print("No active lock. Queue is available for processing.")
                
    except Exception as e:
        print(f"Error checking locks: {str(e)}")

def list_scheduled_tasks():
    """List all tasks scheduled for the next day"""
    print_header("Tasks Scheduled For Tomorrow")
    tomorrow = datetime.now() + timedelta(days=1)
    tomorrow_day = tomorrow.strftime('%A')[:3].lower()
    
    try:
        with sqlite3.connect(get_db_path()) as conn:
            c = conn.cursor()
            
            c.execute("""
                SELECT id, name, schedule, status, processing_status
                FROM tasks
                WHERE schedule LIKE ?
                ORDER BY id
            """, (f'%{tomorrow_day}|%',))
            
            tasks = c.fetchall()
            
            if not tasks:
                print(f"No tasks scheduled for tomorrow ({tomorrow_day}).")
                return
            
            print(f"Found {len(tasks)} tasks scheduled for tomorrow ({tomorrow_day}):")
            print(f"{'ID':<5} {'Name':<30} {'Status':<10} {'Processing':<15} Schedule")
            print("-" * 80)
            
            for task in tasks:
                task_id, name, schedule, status, processing = task
                print(f"{task_id:<5} {name[:28]:<30} {status:<10} {processing:<15} {schedule}")
                
            print()
            print("Times extracted from schedules:")
            print("-" * 80)
            
            # Calculate all the times
            for task in tasks:
                task_id, name, schedule, status, processing = task
                # Parse the schedule and extract times
                day_schedules = schedule.split(';')
                for day_schedule in day_schedules:
                    if not day_schedule.strip() or tomorrow_day not in day_schedule.lower():
                        continue
                    day, times = day_schedule.split('|')
                    time_list = [t.strip() for t in times.split(',')]
                    print(f"Task {task_id} ({name[:20]}): {day.upper()} at {', '.join(time_list)}")
            
    except Exception as e:
        print(f"Error listing scheduled tasks: {str(e)}")

def list_pending_videos():
    """List all pending videos"""
    print_header("Pending Videos")
    
    try:
        with sqlite3.connect(get_db_path()) as conn:
            c = conn.cursor()
            
            # Get all pending videos ordered by scheduled time
            c.execute("""
                SELECT
                    v.id,
                    t.name,
                    v.original_name,
                    v.scheduled_time,
                    v.processed_path,
                    v.status,
                    v.upload_status,
                    v.generated_at,
                    v.retry_count
                FROM generated_videos v
                JOIN tasks t ON v.task_id = t.id
                WHERE v.upload_status = 'pending'
                ORDER BY v.scheduled_time ASC
            """)
            
            videos = c.fetchall()
            
            if not videos:
                print("No pending videos found.")
                return
            
            print(f"Found {len(videos)} pending videos:")
            print(f"{'ID':<5} {'Task':<25} {'Status':<10} {'Upload':<10} {'Scheduled':<20} {'Generated':<20}")
            print("-" * 100)
            
            for video in videos:
                video_id, task_name, original_name, scheduled_time, path, status, upload_status, generated_at, retry_count = video
                
                # Check if the file exists
                file_exists = "✓" if os.path.exists(path) else "✗"
                
                print(f"{video_id:<5} {task_name[:23]:<25} {status:<10} {upload_status:<10} {scheduled_time:<20} {generated_at:<20}")
                print(f"      File: {os.path.basename(path)} ({file_exists}) Retries: {retry_count}")
                print(f"      Original: {original_name}")
                print("-" * 100)
    
    except Exception as e:
        print(f"Error listing pending videos: {str(e)}")

def check_night_window():
    """Check if current time is within night processing window"""
    print_header("Night Processing Window")
    
    # Get the environment variables with defaults
    try:
        start_time_str = os.getenv('NIGHT_PROCESSING_START', '01:30')
        end_time_str = os.getenv('NIGHT_PROCESSING_END', '06:00')
        
        print(f"Night processing window: {start_time_str} - {end_time_str}")
        
        # Parse the times
        start_time = datetime.strptime(start_time_str, '%H:%M').time()
        end_time = datetime.strptime(end_time_str, '%H:%M').time()
        
        # Get current time
        now = datetime.now()
        current_time = now.time()
        
        # Check if we're in the window
        in_window = False
        if start_time < end_time:
            in_window = start_time <= current_time <= end_time
        else:  # Window crosses midnight
            in_window = current_time >= start_time or current_time <= end_time
        
        if in_window:
            print("CURRENT TIME IS WITHIN NIGHT PROCESSING WINDOW")
            print(f"Current time: {current_time.strftime('%H:%M')} ✓")
        else:
            print("Current time is outside the night processing window")
            print(f"Current time: {current_time.strftime('%H:%M')} ✗")
        
        # Calculate time until next night window
        next_start = datetime.combine(now.date(), start_time)
        if current_time > start_time:
            next_start = next_start + timedelta(days=1)
        
        time_to_window = next_start - now
        hours, remainder = divmod(time_to_window.seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        
        print(f"Time until next night window: {hours}h {minutes}m {seconds}s")
        
    except Exception as e:
        print(f"Error checking night window: {str(e)}")

def check_for_errors():
    """Check for recent errors in the logs"""
    print_header("Recent Errors (Last 24 Hours)")
    
    try:
        with sqlite3.connect(get_db_path()) as conn:
            c = conn.cursor()
            
            c.execute("""
                SELECT timestamp, level, message, task_id, details
                FROM logs
                WHERE level IN ('ERROR', 'CRITICAL')
                AND datetime(timestamp) > datetime('now', '-1 day')
                ORDER BY timestamp DESC
                LIMIT 20
            """)
            
            errors = c.fetchall()
            
            if not errors:
                print("No errors found in the last 24 hours! ✓")
                return
            
            print(f"Found {len(errors)} recent errors:")
            print("-" * 100)
            
            for error in errors:
                timestamp, level, message, task_id, details = error
                task_info = f"Task {task_id}" if task_id else "No task"
                
                # Try to parse details JSON if present
                details_str = ""
                if details:
                    try:
                        details_dict = json.loads(details)
                        # Extract the most important details
                        if "error" in details_dict:
                            details_str = f"Error: {details_dict['error']}"
                    except:
                        details_str = details[:50] + "..." if len(details) > 50 else details
                
                print(f"{timestamp} [{level}] {task_info}: {message}")
                if details_str:
                    print(f"  Details: {details_str}")
                print("-" * 100)
    
    except Exception as e:
        print(f"Error checking logs: {str(e)}")

def main():
    """Main function to run all checks"""
    print("\n" + "#" * 100)
    print("#" + " VIDEO PIPELINE MONITOR ".center(98, " ") + "#")
    print("#" * 100)
    print(f"Current time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("#" * 100)
    
    check_task_locks()
    check_night_window()
    list_scheduled_tasks()
    list_pending_videos()
    check_for_errors()
    
    print("\n" + "#" * 100)
    print("# RECOMMENDATIONS:".ljust(99) + "#")
    print("#" * 100)
    print("# 1. To fix stuck locks, run: python scripts/reset_locks.py".ljust(99) + "#")
    print("# 2. To force process videos, run: python scripts/force_process.py".ljust(99) + "#")
    print("# 3. For Docker issues, try: docker-compose down && docker-compose up -d".ljust(99) + "#")
    print("#" * 100)

if __name__ == '__main__':
    main()
