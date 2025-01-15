import subprocess
import time
import glob
import os
import shlex
from datetime import datetime
from app import logger

def cleanup_existing_mp4s():
    """
    Clean up any existing .mp4 files in the current directory
    """
    try:
        for file in glob.glob("*.mp4*"):  # This will catch .mp4, .mp4.1, .mp4.2, etc.
            try:
                os.remove(file)
                logger.info(f"Cleaned up existing file: {file}")
            except Exception as e:
                logger.warning(f"Could not remove file {file}: {e}")
    except Exception as e:
        logger.error(f"Error during cleanup: {e}")

def ensure_directory_writable(directory="."):
    """
    Check if a directory is writable
    
    Args:
        directory (str): Directory to check
        
    Returns:
        bool: True if the directory is writable, False otherwise
    """
    try:
        # Check directory exists and is writable
        if not os.path.exists(directory):
            os.makedirs(directory, exist_ok=True)
            logger.info(f"Created directory: {directory}")
            
        # Verify we can write to the directory
        test_file = os.path.join(directory, '.write_test')
        try:
            with open(test_file, 'w') as f:
                f.write('test')
            os.remove(test_file)
            return True
        except Exception as e:
            logger.error(f"Directory not writable: {directory}: {str(e)}")
            return False
            
    except Exception as e:
        logger.error(f"Error checking directory {directory}: {str(e)}")
        return False

def execute_curl(curl_command, retries=3, retry_delay=1, clean_before=False):
    """
    Execute a CURL command with retries and improved error handling
    
    Args:
        curl_command (str): The curl command to execute
        retries (int): Number of retry attempts
        retry_delay (int): Delay between retries in seconds
        clean_before (bool): Whether to clean up existing .mp4 files before execution
        
    Returns:
        tuple: (success: bool, stdout: str, stderr: str)
    """
    if not ensure_directory_writable():
        return False, "", "Failed to verify directory is writable"

    if clean_before:
        cleanup_existing_mp4s()

    # Always use shell=True for complex curl commands
    for attempt in range(retries):
        try:
            logger.info(f"Attempt {attempt + 1}/{retries}: Executing command: {curl_command}")
            
            process = subprocess.Popen(
                curl_command,
                shell=True,  # Always use shell=True for curl commands
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )

            # Communicate with timeout
            try:
                stdout, stderr = process.communicate(timeout=1200)  # 6-minute timeout
                stdout_str = stdout.decode(errors='replace')
                stderr_str = stderr.decode(errors='replace')
            except subprocess.TimeoutExpired:
                process.kill()
                logger.error("Process timed out")
                return False, "", "Process timed out"

            # Log output
            if stdout_str:
                logger.info(f"Stdout: {stdout_str[:1000]}...")
            
            # Check if command was successful but taking too long
            if "200" in stdout_str and attempt < retries - 1:
                logger.info("Command returned 200 but waiting for file...")
                time.sleep(retry_delay * 5)  # Wait longer between retries
                continue
                
            if stderr_str:
                logger.warning(f"Stderr: {stderr_str}")

            # Check if command was successful
            if process.returncode == 0:
                return True, stdout_str, stderr_str

            logger.error(f"Command failed with return code {process.returncode}")
            
            # Specific error handling
            if "Failed writing header" in stderr_str or "Failed to open" in stderr_str:
                if attempt < retries - 1:
                    logger.info("Retrying due to file writing error...")
                    time.sleep(retry_delay * (attempt + 1))
                    continue
                return False, stdout_str, "Failed writing file"

        except Exception as e:
            logger.error(f"Error executing CURL command (attempt {attempt+1}): {str(e)}")
            logger.exception(e)
            if attempt < retries - 1:
                time.sleep(retry_delay * (attempt + 1))
                continue
            return False, "", f"Error: {str(e)}"

    return False, "", f"Failed after {retries} attempts"

def get_latest_video(max_retries=10, delay=2, min_size_bytes=1024):
    """
    Get the most recently created MP4 file in the current directory with improved validation.
    
    Args:
        max_retries (int): Maximum number of attempts to find the video file
        delay (int): Delay in seconds between attempts
        min_size_bytes (int): Minimum file size to consider valid
        
    Returns:
        str: Path to the video file or None if not found
    """
    start_time = datetime.now()
    
    for attempt in range(max_retries):
        video_files = glob.glob("*.mp4")
        valid_videos = []
        
        for video in video_files:
            try:
                # Check if file is fully written and meets minimum size
                if os.path.exists(video) and os.path.getsize(video) >= min_size_bytes:
                    # Try to open the file to ensure it's not being written to
                    try:
                        with open(video, 'rb') as f:
                            # Read the last byte to ensure file is complete
                            f.seek(-1, 2)
                            f.read(1)
                        valid_videos.append(video)
                    except (IOError, OSError):
                        continue
                        
            except OSError as e:
                logger.warning(f"Error checking video file {video}: {str(e)}")
                continue

        if valid_videos:
            latest_video = max(valid_videos, key=os.path.getctime)
            logger.info(f"Found valid video file: {latest_video} after {attempt + 1} attempts")
            return latest_video

        elapsed = (datetime.now() - start_time).total_seconds()
        if elapsed >= (max_retries * delay):
            logger.error("Timeout waiting for valid video file")
            break
            
        logger.info(f"No valid video file found yet, attempt {attempt + 1} of {max_retries}")
        time.sleep(delay)

    return None

def cleanup_video(video_file):
    """
    Clean up a video file with improved error handling and logging
    
    Args:
        video_file (str): Path to the video file to clean up
    """
    if not video_file:
        return

    try:
        if os.path.exists(video_file):
            file_size = os.path.getsize(video_file)
            logger.info(f"Cleaning up video file: {video_file} (size: {file_size} bytes)")
            
            # Try to ensure file is not in use
            retries = 3
            for i in range(retries):
                try:
                    os.remove(video_file)
                    logger.info(f"Successfully removed video file: {video_file}")
                    break
                except PermissionError:
                    if i < retries - 1:
                        time.sleep(1)
                        continue
                    raise
                    
    except Exception as e:
        logger.error(f"Error removing video file {video_file}: {str(e)}")
        logger.exception(e)

def format_upload_command(cmd_template, video_file, task_data, platform_data):
    """
    Format an upload command with improved parameter handling
    
    Args:
        cmd_template (str): Template string for the command
        video_file (str): Path to the video file
        task_data (dict): Task-specific data
        platform_data (dict): Platform-specific data
        
    Returns:
        str: Formatted command string
    """
    try:
        video_title = os.path.splitext(os.path.basename(video_file))[0]
        
        # Ensure all required keys exist with defaults
        platform_defaults = {
            'account_name': 'default_account',
            'default_hashtags': ''
        }
        platform_data = {**platform_defaults, **platform_data}
        
        task_defaults = {
            'sound_name': 'default',
            'sound_volume': 'background',
            'hashtags': platform_data['default_hashtags']
        }
        task_data = {**task_defaults, **task_data}
        
        # Clean and validate paths
        video_file = os.path.abspath(video_file).replace('\\', '/')  # Convert Windows path to forward slashes
        
        # Log the exact file path being used
        logger.info(f"Using video file path: {video_file}")
        
        # Verify file exists and is readable
        if not os.path.exists(video_file):
            raise FileNotFoundError(f"Video file not found: {video_file}")
        if not os.access(video_file, os.R_OK):
            raise PermissionError(f"Video file not readable: {video_file}")
            
        # Format the command with proper escaping of special characters
        formatted_cmd = cmd_template.format(
            video=video_file,  # Don't strip @ since we handle it in the template
            description=shlex.quote(video_title) if video_title else '',
            account=platform_data['account_name'],
            sound=task_data['sound_name'],
            volume=task_data['sound_volume'],
            hashtags=task_data['hashtags']
        )
        
        # Log the formatted command for debugging
        logger.info(f"Formatted upload command: {formatted_cmd}")
        
        return formatted_cmd
        
    except KeyError as e:
        logger.error(f"Missing required key in template data: {str(e)}")
        raise
    except Exception as e:
        logger.error(f"Error formatting upload command: {str(e)}")
        raise