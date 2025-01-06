from datetime import datetime
import pytz
import os

def get_timezone():
    """Get timezone from environment variable or fall back to UTC"""
    return os.environ.get('TIMEZONE', 'UTC')

def localize_timestamp(timestamp_str):
    """Convert UTC timestamp to local time"""
    if not timestamp_str:
        return None
    
    # Parse the timestamp (assuming it's in UTC)
    try:
        # First try parsing with microseconds
        dt = datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S.%f')
    except ValueError:
        # If that fails, try without microseconds
        dt = datetime.strptime(timestamp_str, '%Y-%m-%d %H:%M:%S')
    
    # Set UTC timezone
    utc_dt = pytz.UTC.localize(dt)
    
    # Convert to local timezone
    local_tz = pytz.timezone(get_timezone())
    local_dt = utc_dt.astimezone(local_tz)
    
    return local_dt.strftime('%Y-%m-%d %H:%M:%S')