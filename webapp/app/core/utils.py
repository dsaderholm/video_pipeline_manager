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

def get_latest_video():
    """Get the most recently created MP4 file in the current directory"""
    video_files = glob.glob("*.mp4")
    if not video_files:
        return None
    return max(video_files, key=os.path.getctime)

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