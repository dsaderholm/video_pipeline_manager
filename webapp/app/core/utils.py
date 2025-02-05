import subprocess
import time
import glob
import os
import shlex
import uuid
import urllib.parse
import re
from datetime import datetime
from app import logger

def cleanup_existing_mp4s():
    try:
        for file in glob.glob("*.mp4*"):
            try:
                os.remove(file)
                logger.info(f"Cleaned up existing file: {file}")
            except Exception as e:
                logger.warning(f"Could not remove file {file}: {e}")
    except Exception as e:
        logger.error(f"Error during cleanup: {e}")

def ensure_directory_writable(directory="."):
    try:
        if not os.path.exists(directory):
            os.makedirs(directory, exist_ok=True)
            logger.info(f"Created directory: {directory}")
            
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

def check_curl_response(stdout_str, stderr_str):
    """
    Check curl response for success/failure indicators
    Returns: (bool, str) - (success, error_message)
    """
    # Check for HTTP response codes
    http_code_match = re.search(r'HTTP/\d\.\d\s+(\d{3})', stdout_str + stderr_str)
    if http_code_match:
        code = int(http_code_match.group(1))
        if code >= 400:
            return False, f"HTTP error {code}"
        if code >= 200 and code < 300:
            return True, ""
    
    # Check for common curl errors
    error_patterns = [
        (r'curl:\s*\(\d+\)', True),  # Curl error codes
        (r'Connection refused', True),
        (r'Could not resolve host', True),
        (r'Failed to connect', True),
        (r'Operation timed out', True),
        (r'SSL certificate problem', True),
        (r'error:', True),
        (r'HTTP/\d\.\d 5\d{2}', True),  # Server errors
        (r'HTTP/\d\.\d 4\d{2}', True),  # Client errors
        (r'success|successful', False),  # Success indicators
        (r'HTTP/\d\.\d 2\d{2}', False)  # Success codes
    ]
    
    for pattern, is_error in error_patterns:
        if re.search(pattern, stdout_str + stderr_str, re.IGNORECASE):
            if is_error:
                return False, f"Found error pattern: {pattern}"
            else:
                return True, ""
    
    # If no clear success/error indicators, check if stderr is empty
    if stderr_str.strip():
        return False, "Unexpected error in stderr"
    
    return True, ""

def execute_curl(curl_command, retries=3, retry_delay=1, clean_before=False):
    if not ensure_directory_writable():
        return False, "", "Failed to verify directory is writable"

    if clean_before:
        cleanup_existing_mp4s()

    for attempt in range(retries):
        try:
            logger.info(f"Attempt {attempt + 1}/{retries}: Executing command: {curl_command}")
            
            process = subprocess.Popen(
                curl_command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )

            try:
                stdout, stderr = process.communicate(timeout=1200)
                stdout_str = stdout.decode(errors='replace')
                stderr_str = stderr.decode(errors='replace')
            except subprocess.TimeoutExpired:
                process.kill()
                logger.error("Process timed out")
                return False, "", "Process timed out"

            if stdout_str:
                logger.info(f"Stdout: {stdout_str[:1000]}...")
            if stderr_str:
                logger.warning(f"Stderr: {stderr_str}")

            # Check curl response
            success, error_msg = check_curl_response(stdout_str, stderr_str)
            if success:
                if "200" in stdout_str and attempt < retries - 1:
                    logger.info("Command returned 200 but waiting for file...")
                    time.sleep(retry_delay * 5)
                    continue
                return True, stdout_str, stderr_str

            logger.error(f"Command failed: {error_msg}")
            if attempt < retries - 1:
                retry_delay_time = retry_delay * (2 ** attempt)  # Exponential backoff
                logger.info(f"Retrying in {retry_delay_time} seconds...")
                time.sleep(retry_delay_time)
                continue

        except Exception as e:
            logger.error(f"Error executing CURL command (attempt {attempt+1}): {str(e)}")
            logger.exception(e)
            if attempt < retries - 1:
                time.sleep(retry_delay * (attempt + 1))
                continue
            return False, "", f"Error: {str(e)}"

    return False, "", f"Failed after {retries} attempts"

def get_latest_video(max_retries=10, delay=2, min_size_bytes=1024):
    start_time = datetime.now()
    
    for attempt in range(max_retries):
        video_files = glob.glob("*.mp4")
        valid_videos = []
        
        for video in video_files:
            try:
                if os.path.exists(video) and os.path.getsize(video) >= min_size_bytes:
                    try:
                        with open(video, 'rb') as f:
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
    if not video_file:
        return

    try:
        if os.path.exists(video_file):
            file_size = os.path.getsize(video_file)
            logger.info(f"Cleaning up video file: {video_file} (size: {file_size} bytes)")
            
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

def create_safe_filename(original_path):
    directory = os.path.dirname(original_path) or '.'
    original_name = os.path.basename(original_path)
    safe_name = f"upload_{uuid.uuid4().hex[:8]}.mp4"
    return os.path.join(directory, safe_name), original_name

def format_upload_command(cmd_template, video_file, task_data, platform_data):
    try:
        safe_video_path, original_name = create_safe_filename(video_file)
        try:
            os.rename(video_file, safe_video_path)
            logger.info(f"Renamed '{video_file}' to '{safe_video_path}' for safe upload")
        except OSError as e:
            logger.error(f"Failed to rename video file: {e}")
            return None, None

        video_title = os.path.splitext(original_name)[0]
        
        platform_defaults = {
            'account_name': 'default_account',
            'default_hashtags': ''
        }
        platform_data = {**platform_defaults, **(platform_data or {})}
        
        task_defaults = {
            'sound_name': 'default',
            'sound_volume': 'background',
            'hashtags': platform_data['default_hashtags']
        }
        task_data = {**task_defaults, **(task_data or {})}

        hashtags = task_data['hashtags']
        if hashtags:
            tags = [tag.strip() for tag in hashtags.split() if tag.strip()]
            tags = [tag if tag.startswith('#') else f'#{tag}' for tag in tags]
            hashtags = ' '.join(tags)

        formatted_cmd = cmd_template.format(
            video=safe_video_path,
            description=urllib.parse.quote(video_title),
            account=urllib.parse.quote(platform_data['account_name']),
            sound=urllib.parse.quote(task_data['sound_name']),
            volume=task_data['sound_volume'],
            hashtags=urllib.parse.quote(hashtags)
        )
        
        logger.info(f"Formatted upload command: {formatted_cmd}")
        return formatted_cmd, safe_video_path
        
    except KeyError as e:
        logger.error(f"Missing required key in template data: {str(e)}")
        raise
    except Exception as e:
        logger.error(f"Error formatting upload command: {str(e)}")
        raise