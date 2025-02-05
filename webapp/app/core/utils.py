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

def validate_video_file(file_path, min_size_bytes=1024):
    """
    Validate a video file by checking its header and structure
    """
    if not os.path.exists(file_path):
        return False, "File does not exist"
        
    if os.path.getsize(file_path) < min_size_bytes:
        return False, f"File too small (< {min_size_bytes} bytes)"
        
    try:
        # Check MP4 file signature (first 8 bytes)
        with open(file_path, 'rb') as f:
            header = f.read(8)
            # Valid MP4 signatures: ftyp, mdat, moov, free, wide, skip
            if not any(sig in header for sig in [b'ftyp', b'mdat', b'moov', b'free', b'wide', b'skip']):
                return False, "Invalid MP4 header"
            
            # Check if file is readable till the end
            f.seek(-1, 2)
            f.read(1)
            
            # Run ffprobe to check video stream
            result = subprocess.run(
                ['ffprobe', '-v', 'error', file_path],
                stderr=subprocess.PIPE,
                stdout=subprocess.PIPE
            )
            
            if result.returncode != 0:
                return False, f"FFprobe validation failed: {result.stderr.decode()}"
                
            return True, "Video file is valid"
            
    except Exception as e:
        return False, f"Validation error: {str(e)}"

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
        (r'HTTP/\d\.\d 4\d{2}', True)  # Client errors
    ]
    
    # Check for success patterns
    success_patterns = [
        r'HTTP/\d\.\d 2\d{2}',  # Success codes
        r'success|successful'    # Success indicators
    ]

    # First check for success patterns
    for pattern in success_patterns:
        if re.search(pattern, stdout_str + stderr_str, re.IGNORECASE):
            return True, ""

    # Then check for error patterns
    for pattern, is_error in error_patterns:
        if re.search(pattern, stdout_str + stderr_str, re.IGNORECASE):
            return False, f"Found error pattern: {pattern}"
    
    # Check if stderr contains only Werkzeug logs
    stderr_lines = stderr_str.strip().split('\n')
    non_werkzeug_lines = [line for line in stderr_lines if line.strip() and not line.strip().startswith('INFO:werkzeug:')]
    
    if non_werkzeug_lines:
        return False, "Unexpected error in stderr"
    
    return True, ""

def execute_curl(curl_command, retries=3, retry_delay=1, clean_before=False, validate_output=False):
    """
    Execute a CURL command with retries and improved error handling
    
    Args:
        curl_command (str): The curl command to execute
        retries (int): Number of retry attempts
        retry_delay (int): Delay between retries in seconds
        clean_before (bool): Whether to clean up existing .mp4 files before execution
        validate_output (bool): Whether to validate video output after execution
        
    Returns:
        tuple: (success: bool, stdout: str, stderr: str)
    """
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

            success, error_msg = check_curl_response(stdout_str, stderr_str)
            if success:
                if validate_output:
                    video_file = get_latest_video()
                    if video_file:
                        is_valid, validation_msg = validate_video_file(video_file)
                        if not is_valid:
                            if attempt < retries - 1:
                                logger.error(f"Video validation failed: {validation_msg}")
                                time.sleep(retry_delay * (2 ** attempt))
                                continue
                            return False, stdout_str, f"Invalid video file: {validation_msg}"
                    else:
                        if attempt < retries - 1:
                            logger.error("No video file found after command execution")
                            time.sleep(retry_delay * (2 ** attempt))
                            continue
                        return False, stdout_str, "No video file generated"

                return True, stdout_str, stderr_str

            logger.error(f"Command failed: {error_msg}")
            if attempt < retries - 1:
                retry_delay_time = retry_delay * (2 ** attempt)
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

def create_safe_filename(original_path):
    """
    Creates a safe temporary filename while preserving the original name for reference.
    
    Args:
        original_path (str): Original file path
        
    Returns:
        tuple: (safe_path, original_name)
    """
    directory = os.path.dirname(original_path) or '.'
    original_name = os.path.basename(original_path)
    safe_name = f"upload_{uuid.uuid4().hex[:8]}.mp4"
    return os.path.join(directory, safe_name), original_name

def format_upload_command(cmd_template, video_file, task_data, platform_data):
    """
    Format an upload command with improved parameter handling and safe file operations
    
    Args:
        cmd_template (str): Template string for the command
        video_file (str): Path to the video file
        task_data (dict): Task-specific data
        platform_data (dict): Platform-specific data
        
    Returns:
        tuple: (formatted_command: str, safe_video_path: str)
    """
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

        # Clean hashtags by removing any empty tags and ensuring proper format
        hashtags = task_data['hashtags']
        if hashtags:
            # Split by spaces, remove empty strings, ensure # prefix
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