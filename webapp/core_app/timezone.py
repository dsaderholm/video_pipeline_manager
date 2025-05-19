from datetime import datetime
import pytz
import os

def get_timezone():
    """Get timezone from environment variable or fall back to UTC"""
    return os.environ.get('TIMEZONE', 'UTC')

def localize_timestamp(timestamp):
    """Convert UTC timestamp to local time.
    
    Works with both string timestamps (from SQLite) and 
    datetime objects (from PostgreSQL).
    """
    if not timestamp:
        return None
    
    # Check if we already have a datetime object (from PostgreSQL)
    if isinstance(timestamp, datetime):
        dt = timestamp
    else:
        # Parse the timestamp string (from SQLite)
        try:
            # First try ISO format
            dt = datetime.fromisoformat(timestamp)
        except ValueError:
            try:
                # Then try parsing with microseconds
                dt = datetime.strptime(timestamp, '%Y-%m-%d %H:%M:%S.%f')
            except ValueError:
                # If that fails, try without microseconds
                dt = datetime.strptime(timestamp, '%Y-%m-%d %H:%M:%S')
    
    # Handle timezone - assume naive datetime is in UTC
    if dt.tzinfo is None:
        # Set UTC timezone
        utc_dt = pytz.UTC.localize(dt)
    else:
        # Already has timezone info
        utc_dt = dt
    
    # Convert to local timezone
    local_tz = pytz.timezone(get_timezone())
    local_dt = utc_dt.astimezone(local_tz)
    
    return local_dt.strftime('%Y-%m-%d %H:%M:%S')