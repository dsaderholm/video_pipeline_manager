import subprocess
import time
import glob
import os
from app import logger

def execute_curl(curl_command, retries=3):
    """Execute a CURL command with retries"""
    for attempt in range(retries):
        try:
            logger.info(f"Attempt {attempt + 1}: Executing command: {curl_command}")
            process = subprocess.Popen(curl_command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            stdout, stderr = process.communicate()
            stdout_str = stdout.decode()
            stderr_str = stderr.decode()
            
            logger.info(f"Stdout: {stdout_str}")
            if stderr_str:
                logger.error(f"Stderr: {stderr_str}")
            
            if process.returncode == 0:
                return True, stdout_str, stderr_str
                
            logger.error(f"Command failed with return code {process.returncode}")
            time.sleep(attempt + 1)  # Exponential backoff
            
        except Exception as e:
            logger.error(f"Error executing CURL command (attempt {attempt+1}): {str(e)}")
            logger.exception(e)  # This logs the full stack trace
            time.sleep(attempt + 1)
            
    return False, "", f"Failed after {retries} attempts"

def get_latest_video(max_retries=10, delay=2):
    """
    Get the most recently created MP4 file in the current directory.
    Retries multiple times with delays to handle download timing issues.
    
    Args:
        max_retries (int): Maximum number of attempts to find the video file
        delay (int): Delay in seconds between attempts
    """
    for attempt in range(max_retries):
        video_files = glob.glob("*.mp4")
        if video_files:
            latest_video = max(video_files, key=os.path.getctime)
            # Check if file is fully written
            if os.path.getsize(latest_video) > 0:
                logger.info(f"Found video file: {latest_video} after {attempt + 1} attempts")
                return latest_video
                
        logger.info(f"No video file found yet, attempt {attempt + 1} of {max_retries}")
        time.sleep(delay)  # Wait before next attempt
        
    return None

def cleanup_video(video_file):
    """Clean up a video file if it exists"""
    try:
        if video_file and os.path.exists(video_file):
            os.remove(video_file)
    except Exception as e:
        logger.error(f"Error removing video file: {str(e)}")

def format_upload_command(cmd_template, video_file, task_data, platform_data):
    """Format an upload command with all necessary parameters"""
    video_title = os.path.splitext(video_file)[0]
    return cmd_template.format(
        video=video_file,
        description=video_title,
        account=platform_data['account_name'],
        sound=task_data.get('sound_name', 'default'),
        volume=task_data.get('sound_volume', 'background'),
        hashtags=task_data.get('hashtags') or platform_data.get('default_hashtags', '')
    )