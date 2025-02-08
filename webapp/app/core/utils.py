import subprocess
import time
import glob
import os
import shlex
import uuid
import urllib.parse
import re
import json
from datetime import datetime
from app import logger
import sys

def log_with_details(level, message, task_id=None, details=None, source=None):
    """Helper function to log with structured details"""
    try:
        from app.core.log_manager import add_log_entry
        add_log_entry(level, message, task_id=task_id, details=details, source=source)
    except Exception as e:
        # Fallback logging to stderr if database logging fails
        print(f"ERROR: Failed to log to database: {str(e)}", file=sys.stderr)
        print(f"{level}: {message} (Task {task_id})", file=sys.stderr)
        if details:
            print(f"Details: {json.dumps(details, default=str)}", file=sys.stderr)

def parse_curl_response(stdout_str, stderr_str):
    """Parse curl response and extract HTTP status code, headers, and body"""
    response = {
        'status_code': None,
        'headers': {},
        'body': '',
        'error': None,
        'platform_specific': {}
    }
    
    # First, look for HTTP status line
    status_match = re.search(r'HTTP/\d\.\d\s+(\d{3})', stdout_str + stderr_str)
    if status_match:
        response['status_code'] = int(status_match.group(1))
    
    # Look for JSON response in stdout
    try:
        json_start = stdout_str.find('{')
        if json_start != -1:
            json_content = stdout_str[json_start:]
            response['body'] = json.loads(json_content)
    except json.JSONDecodeError:
        # If not JSON, store raw content
        response['body'] = stdout_str.strip()
    
    # Check for platform-specific patterns
    platform_patterns = {
        'tiktok': {
            'error_code': r'error_code["\']:\s*(\d+)',
            'error_message': r'error_message["\']:\s*["\']([^"\']+)',
            'success_pattern': r'share_url["\']:\s*["\']([^"\']+)'
        },
        'instagram': {
            'error_type': r'error_type["\']:\s*["\']([^"\']+)',
            'media_id': r'media_id["\']:\s*["\']([^"\']+)'
        },
        'youtube': {
            'error': r'error["\']:\s*{([^}]+)}',
            'video_id': r'videoId["\']:\s*["\']([^"\']+)'
        }
    }
    
    for platform, patterns in platform_patterns.items():
        platform_data = {}
        for key, pattern in patterns.items():
            match = re.search(pattern, stdout_str)
            if match:
                platform_data[key] = match.group(1)
        if platform_data:
            response['platform_specific'][platform] = platform_data
    
    return response

def check_curl_response(stdout_str, stderr_str):
    """Enhanced curl response checking with better error handling"""
    response = parse_curl_response(stdout_str, stderr_str)
    
    # Log the complete response for debugging
    log_with_details('DEBUG', "Curl response analysis", details={
        'stdout_preview': stdout_str[:1000],
        'stderr_preview': stderr_str[:1000],
        'parsed_response': response
    })
    
    # Check for explicitly successful download
    if 'Downloaded' in stderr_str or 'saved' in stderr_str:
        return True, ""
    
    # Check for known success patterns
    if response['status_code']:
        if response['status_code'] >= 500:
            return False, f"Server error: HTTP {response['status_code']}"
        if response['status_code'] >= 400:
            return False, f"Client error: HTTP {response['status_code']}"
        if response['status_code'] >= 200 and response['status_code'] < 300:
            # Additional platform-specific success validation
            for platform, data in response.get('platform_specific', {}).items():
                if platform == 'tiktok' and 'error_code' in data:
                    return False, f"TikTok API error: {data.get('error_message', 'Unknown error')}"
                if platform == 'instagram' and 'error_type' in data:
                    return False, f"Instagram API error: {data['error_type']}"
                if platform == 'youtube' and 'error' in data:
                    return False, f"YouTube API error: {data['error']}"
            return True, ""
    
    # Check common error patterns
    error_patterns = [
        (r'curl:\s*\(\d+\)', "Curl error"),
        (r'Connection refused', "Connection refused"),
        (r'Could not resolve host', "DNS resolution failed"),
        (r'Operation timed out', "Request timed out"),
        (r'SSL certificate problem', "SSL verification failed"),
        (r'The requested URL returned error: ([45]\d{2})', "HTTP error {0}"),
        (r'Failed to connect', "Connection failed"),
        (r'error:', "Generic error")
    ]
    
    for pattern, error_msg in error_patterns:
        match = re.search(pattern, stdout_str + stderr_str, re.IGNORECASE)
        if match:
            if len(match.groups()) > 0:
                return False, error_msg.format(*match.groups())
            return False, error_msg
    
    # If no error is found in stderr and it only contains progress info, consider it successful
    if not stderr_str.strip() or all(
        line.startswith(('* ', '  % Total', '100  ', 'Warning: ')) 
        for line in stderr_str.strip().split('\n')
    ):
        return True, ""
    
    return False, "Unknown error occurred"

def execute_curl(curl_command, retries=3, retry_delay=1, clean_before=False, validate_output=False, timeout=3600):
    """Enhanced curl execution with better error handling and retry logic"""
    execution_details = {
        'command': curl_command,
        'attempts': [],
        'start_time': datetime.now().isoformat()
    }
    
    if clean_before:
        cleanup_existing_mp4s()
    
    for attempt in range(retries):
        attempt_details = {
            'attempt': attempt + 1,
            'timestamp': datetime.now().isoformat()
        }
        
        try:
            process = subprocess.Popen(
                curl_command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            
            stdout, stderr = process.communicate(timeout=timeout)
            stdout_str = stdout.decode(errors='replace')
            stderr_str = stderr.decode(errors='replace')
            
            attempt_details.update({
                'return_code': process.returncode,
                'stdout_length': len(stdout_str),
                'stderr_length': len(stderr_str)
            })
            
            success, error_msg = check_curl_response(stdout_str, stderr_str)
            attempt_details['success'] = success
            attempt_details['error_message'] = error_msg
            
            if success:
                if validate_output:
                    video_file = get_latest_video()
                    if not video_file:
                        if attempt < retries - 1:
                            time.sleep(retry_delay * (2 ** attempt))
                            continue
                        return False, stdout_str, "No video file generated"
                    
                    is_valid, validation_msg = validate_video_file(video_file)
                    if not is_valid:
                        if attempt < retries - 1:
                            time.sleep(retry_delay * (2 ** attempt))
                            continue
                        return False, stdout_str, f"Invalid video file: {validation_msg}"
                
                execution_details['attempts'].append(attempt_details)
                log_with_details('INFO', "Curl command executed successfully", details=execution_details)
                return True, stdout_str, stderr_str
            
            if attempt < retries - 1:
                retry_delay_time = retry_delay * (2 ** attempt)
                time.sleep(retry_delay_time)
                execution_details['attempts'].append(attempt_details)
                continue
            
            return False, stdout_str, error_msg
            
        except subprocess.TimeoutExpired:
            attempt_details['error'] = "Process timed out"
            execution_details['attempts'].append(attempt_details)
            if attempt < retries - 1:
                continue
            return False, "", "Process timed out"
            
        except Exception as e:
            attempt_details['error'] = str(e)
            execution_details['attempts'].append(attempt_details)
            if attempt < retries - 1:
                continue
            return False, "", f"Error: {str(e)}"
    
    log_with_details('ERROR', f"Failed after {retries} attempts", details=execution_details)
    return False, "", f"Failed after {retries} attempts"

def validate_video_file(file_path, min_size_bytes=1024):
    """Validate video file by checking its header and structure"""
    validation_details = {
        'file_path': file_path,
        'min_size_bytes': min_size_bytes,
        'checks': []
    }
    
    def add_check(name, result, message=None):
        validation_details['checks'].append({
            'name': name,
            'result': result,
            'message': message
        })
        return result
    
    if not add_check('file_exists', os.path.exists(file_path)):
        log_with_details('ERROR', "Video file validation failed: File does not exist",
            details=validation_details)
        return False, "File does not exist"
    
    file_size = os.path.getsize(file_path)
    validation_details['file_size'] = file_size
    
    if not add_check('size_check', file_size >= min_size_bytes):
        log_with_details('ERROR', "Video file validation failed: File too small",
            details=validation_details)
        return False, f"File too small (< {min_size_bytes} bytes)"
    
    try:
        with open(file_path, 'rb') as f:
            # Check MP4 file signature
            header = f.read(8)
            has_valid_header = any(sig in header for sig in [b'ftyp', b'mdat', b'moov', b'free', b'wide', b'skip'])
            
            if not add_check('header_check', has_valid_header):
                log_with_details('ERROR', "Video file validation failed: Invalid MP4 header",
                    details=validation_details)
                return False, "Invalid MP4 header"
            
            # Check if file is readable till the end
            f.seek(-1, 2)
            f.read(1)
            add_check('read_check', True)
            
            # Run ffprobe to check video stream
            try:
                result = subprocess.run(
                    ['ffprobe', '-v', 'error', file_path],
                    stderr=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    timeout=30
                )
                
                ffprobe_success = result.returncode == 0
                add_check('ffprobe_check', ffprobe_success, 
                         result.stderr.decode() if result.stderr else None)
                
                if not ffprobe_success:
                    log_with_details('ERROR', "Video file validation failed: FFprobe validation error",
                        details=validation_details)
                    return False, f"FFprobe validation failed: {result.stderr.decode()}"
                
            except subprocess.TimeoutExpired:
                log_with_details('ERROR', "Video file validation failed: FFprobe timeout",
                    details=validation_details)
                return False, "FFprobe validation timed out"
            
            log_with_details('INFO', "Video file validation successful",
                details=validation_details)
            return True, "Video file is valid"
            
    except Exception as e:
        validation_details['error'] = str(e)
        log_with_details('ERROR', "Video file validation failed with exception",
            details=validation_details)
        return False, f"Validation error: {str(e)}"

def cleanup_existing_mp4s():
    """Clean up existing MP4 files with improved error handling"""
    cleanup_details = {
        'files_found': [],
        'files_removed': [],
        'errors': []
    }
    
    try:
        for file in glob.glob("*.mp4*"):
            cleanup_details['files_found'].append(file)
            try:
                if os.path.exists(file):
                    os.remove(file)
                    cleanup_details['files_removed'].append(file)
                    log_with_details('INFO', f"Cleaned up existing file: {file}",
                        details={'file': file, 'success': True})
            except Exception as e:
                error_detail = {'file': file, 'error': str(e)}
                cleanup_details['errors'].append(error_detail)
                log_with_details('WARNING', f"Could not remove file {file}",
                    details=error_detail)
                
        if cleanup_details['files_removed']:
            log_with_details('INFO', f"Cleanup completed successfully",
                details=cleanup_details)
                
    except Exception as e:
        cleanup_details['error'] = str(e)
        log_with_details('ERROR', f"Error during cleanup",
            details=cleanup_details)

def get_latest_video(max_retries=10, delay=2, min_size_bytes=1024):
    """Get most recently created valid MP4 file"""
    start_time = datetime.now()
    search_details = {
        'max_retries': max_retries,
        'delay': delay,
        'min_size_bytes': min_size_bytes,
        'start_time': start_time.isoformat()
    }
    
    for attempt in range(max_retries):
        video_files = glob.glob("*.mp4")
        search_details.update({
            'attempt': attempt + 1,
            'found_files': len(video_files),
            'files': video_files
        })
        
        log_with_details('INFO', f"Searching for valid video files (attempt {attempt + 1}/{max_retries})",
            details=search_details)
        
        valid_videos = []
        for video in video_files:
            try:
                if os.path.exists(video) and os.path.getsize(video) >= min_size_bytes:
                    is_valid, _ = validate_video_file(video)
                    if is_valid:
                        valid_videos.append(video)
            except OSError as e:
                search_details.update({
                    'failed_file': video,
                    'error': str(e)
                })
                log_with_details('WARNING', f"Error checking video file {video}",
                    details=search_details)
                continue

        if valid_videos:
            latest_video = max(valid_videos, key=os.path.getctime)
            search_details.update({
                'valid_videos': valid_videos,
                'selected_video': latest_video,
                'success': True
            })
            log_with_details('INFO', f"Found valid video file: {latest_video}",
                details=search_details)
            return latest_video

        elapsed = (datetime.now() - start_time).total_seconds()
        if elapsed >= (max_retries * delay):
            search_details.update({
                'timeout': True,
                'elapsed_seconds': elapsed
            })
            log_with_details('ERROR', "Timeout waiting for valid video file",
                details=search_details)
            break
            
        log_with_details('INFO', f"No valid video file found yet",
            details=search_details)
        time.sleep(delay)

    log_with_details('ERROR', "Failed to find valid video file",
        details=search_details)
    return None

def cleanup_video(video_file):
    """Clean up a video file with improved error handling"""
    if not video_file:
        return

    cleanup_details = {
        'video_file': video_file,
        'exists': False,
        'size': None,
        'success': False
    }

    try:
        if os.path.exists(video_file):
            cleanup_details['exists'] = True
            file_size = os.path.getsize(video_file)
            cleanup_details['size'] = file_size
            
            log_with_details('INFO', f"Cleaning up video file: {video_file}",
                details=cleanup_details)
            
            retries = 3
            for i in range(retries):
                try:
                    os.remove(video_file)
                    cleanup_details['success'] = True
                    cleanup_details['attempts'] = i + 1
                    log_with_details('INFO', f"Successfully removed video file",
                        details=cleanup_details)
                    break
                except PermissionError as e:
                    if i < retries - 1:
                        cleanup_details['current_attempt'] = i + 1
                        cleanup_details['error'] = str(e)
                        log_with_details('WARNING', f"Permission error, retrying...",
                            details=cleanup_details)
                        time.sleep(1)
                        continue
                    raise
                    
    except Exception as e:
        cleanup_details['error'] = str(e)
        cleanup_details['success'] = False
        log_with_details('ERROR', f"Error removing video file",
            details=cleanup_details)

def create_safe_filename(original_path):
    """Create a safe temporary filename for uploads"""
    directory = os.path.dirname(original_path) or '.'
    original_name = os.path.basename(original_path)
    safe_name = f"upload_{uuid.uuid4().hex[:8]}.mp4"
    safe_path = os.path.join(directory, safe_name)
    
    filename_details = {
        'original_path': original_path,
        'directory': directory,
        'original_name': original_name,
        'safe_name': safe_name,
        'safe_path': safe_path
    }
    
    log_with_details('INFO', f"Created safe filename",
        details=filename_details)
    return safe_path, original_name

def format_upload_command(cmd_template, video_file, task_data, platform_data):
    """Format an upload command with improved error handling and validation"""
    try:
        safe_video_path, original_name = create_safe_filename(video_file)
        try:
            os.rename(video_file, safe_video_path)
        except OSError as e:
            log_with_details('ERROR', f"Failed to rename video file: {str(e)}", 
                details={
                    'video_file': video_file,
                    'safe_path': safe_video_path,
                    'error': str(e)
                })
            return None, None

        # Extract video title without extension
        video_title = os.path.splitext(original_name)[0]
        
        # Set default values for platform data
        platform_defaults = {
            'account_name': 'default_account',
            'default_hashtags': ''
        }
        platform_data = {**platform_defaults, **(platform_data or {})}
        
        # Set default values for task data
        task_defaults = {
            'sound_name': 'default',
            'sound_volume': 'background',
            'hashtags': platform_data['default_hashtags']
        }
        task_data = {**task_defaults, **(task_data or {})}

        # Process hashtags
        hashtags = task_data['hashtags']
        if hashtags:
            tags = [tag.strip() for tag in hashtags.split() if tag.strip()]
            tags = [tag if tag.startswith('#') else f'#{tag}' for tag in tags]
            hashtags = ' '.join(tags)

        # Escape special characters in the video file path
        escaped_video_path = safe_video_path.replace(' ', '\\ ')
        
        # Format the command with all required parameters
        formatted_cmd = cmd_template.format(
            video=escaped_video_path,
            description=video_title,
            account=platform_data['account_name'],
            sound=task_data['sound_name'],
            volume=task_data['sound_volume'],
            hashtags=hashtags
        )
        
        log_with_details('INFO', "Successfully formatted upload command", 
            details={
                'formatted_command': formatted_cmd,
                'safe_video_path': safe_video_path,
                'video_title': video_title,
                'platform': platform_data,
                'task': task_data
            })
        
        return formatted_cmd, safe_video_path
        
    except KeyError as e:
        log_with_details('ERROR', f"Missing required parameter: {str(e)}", 
            details={
                'platform_data': platform_data,
                'task_data': task_data,
                'error': str(e)
            })
        raise
    except Exception as e:
        log_with_details('ERROR', f"Error formatting upload command: {str(e)}", 
            details={
                'platform_data': platform_data,
                'task_data': task_data,
                'error': str(e)
            })
        raise