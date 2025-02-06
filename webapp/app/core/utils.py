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
from app.core.log_manager import add_log_entry

def log_with_details(level, message, task_id=None, details=None, source=None):
    """Helper function to log with structured details"""
    # Only add to database, don't duplicate to standard logger
    add_log_entry(level, message, task_id=task_id, details=details, source=source)

def validate_video_file(file_path, min_size_bytes=1024):
    """Validate a video file by checking its header and structure"""
    validation_details = {
        'file_path': file_path,
        'min_size_bytes': min_size_bytes
    }
    
    if not os.path.exists(file_path):
        log_with_details('ERROR', f"Video file validation failed: File does not exist",
            details=validation_details)
        return False, "File does not exist"
        
    file_size = os.path.getsize(file_path)
    validation_details['actual_size'] = file_size
    
    if file_size < min_size_bytes:
        log_with_details('ERROR', f"Video file validation failed: File too small",
            details=validation_details)
        return False, f"File too small (< {min_size_bytes} bytes)"
        
    try:
        # Check MP4 file signature (first 8 bytes)
        with open(file_path, 'rb') as f:
            header = f.read(8)
            validation_details['has_valid_header'] = any(sig in header for sig in [b'ftyp', b'mdat', b'moov', b'free', b'wide', b'skip'])
            
            if not validation_details['has_valid_header']:
                log_with_details('ERROR', "Video file validation failed: Invalid MP4 header",
                    details=validation_details)
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
            
            validation_details.update({
                'ffprobe_return_code': result.returncode,
                'ffprobe_error': result.stderr.decode() if result.stderr else None
            })
            
            if result.returncode != 0:
                log_with_details('ERROR', f"Video file validation failed: FFprobe validation error",
                    details=validation_details)
                return False, f"FFprobe validation failed: {result.stderr.decode()}"
            
            log_with_details('INFO', "Video file validation successful",
                details=validation_details)    
            return True, "Video file is valid"
            
    except Exception as e:
        validation_details['error'] = str(e)
        log_with_details('ERROR', f"Video file validation failed with exception",
            details=validation_details)
        return False, f"Validation error: {str(e)}"

def cleanup_existing_mp4s():
    """Clean up any existing .mp4 files in the current directory"""
    cleanup_details = {'files_found': [], 'files_removed': [], 'errors': []}
    try:
        for file in glob.glob("*.mp4*"):  # This will catch .mp4, .mp4.1, .mp4.2, etc.
            cleanup_details['files_found'].append(file)
            try:
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

def ensure_directory_writable(directory="."):
    """Check if a directory is writable"""
    check_details = {
        'directory': directory,
        'exists': False,
        'created': False,
        'writable': False
    }
    
    try:
        if not os.path.exists(directory):
            os.makedirs(directory, exist_ok=True)
            check_details['created'] = True
            log_with_details('INFO', f"Created directory: {directory}",
                details=check_details)
        
        check_details['exists'] = True
        test_file = os.path.join(directory, '.write_test')
        try:
            with open(test_file, 'w') as f:
                f.write('test')
            os.remove(test_file)
            check_details['writable'] = True
            log_with_details('INFO', f"Directory {directory} is writable",
                details=check_details)
            return True
        except Exception as e:
            check_details['error'] = str(e)
            log_with_details('ERROR', f"Directory not writable: {directory}",
                details=check_details)
            return False
            
    except Exception as e:
        check_details['error'] = str(e)
        log_with_details('ERROR', f"Error checking directory {directory}",
            details=check_details)
        return False

def check_curl_response(stdout_str, stderr_str):
    """Check curl response for success/failure indicators"""
    response_details = {
        'stdout_preview': stdout_str[:500] + ('...' if len(stdout_str) > 500 else ''),
        'stderr_preview': stderr_str[:500] + ('...' if len(stderr_str) > 500 else ''),
        'patterns_checked': []
    }
    
    # Check for HTTP response codes
    http_code_match = re.search(r'HTTP/\d\.\d\s+(\d{3})', stdout_str + stderr_str)
    if http_code_match:
        code = int(http_code_match.group(1))
        response_details['http_code'] = code
        if code >= 400:
            log_with_details('ERROR', f"CURL HTTP error {code}",
                details=response_details)
            return False, f"HTTP error {code}"
        if code >= 200 and code < 300:
            log_with_details('INFO', f"CURL successful with HTTP {code}",
                details=response_details)
            return True, ""
    
    # Check for success patterns first
    success_patterns = [
        r'HTTP/\d\.\d 2\d{2}',  # Success codes
        r'success|successful'    # Success indicators
    ]
    
    for pattern in success_patterns:
        response_details['patterns_checked'].append({
            'pattern': pattern,
            'type': 'success',
            'matched': bool(re.search(pattern, stdout_str + stderr_str, re.IGNORECASE))
        })
        if re.search(pattern, stdout_str + stderr_str, re.IGNORECASE):
            log_with_details('INFO', "CURL successful based on pattern match",
                details=response_details)
            return True, ""

    # Check for error patterns
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

    for pattern, is_error in error_patterns:
        matched = bool(re.search(pattern, stdout_str + stderr_str, re.IGNORECASE))
        response_details['patterns_checked'].append({
            'pattern': pattern,
            'type': 'error',
            'matched': matched
        })
        if matched:
            log_with_details('ERROR', f"CURL error pattern matched: {pattern}",
                details=response_details)
            return False, f"Found error pattern: {pattern}"
    
    # Check stderr for non-acceptable output
    stderr_lines = stderr_str.strip().split('\n')
    non_acceptable_lines = []
    for line in stderr_lines:
        line = line.strip()
        if line and not any([
            line.startswith('INFO:werkzeug:'),  # Werkzeug logs
            line.startswith('  % Total'),       # Curl progress
            re.match(r'\s*\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+', line),  # Progress numbers
            '--:--:--' in line,                 # Progress timing
            'Speed' in line                     # Progress speed
        ]):
            non_acceptable_lines.append(line)
    
    response_details['non_acceptable_lines'] = non_acceptable_lines
    
    if non_acceptable_lines:
        log_with_details('ERROR', "CURL produced unexpected stderr output",
            details=response_details)
        return False, "Unexpected error in stderr"
    
    log_with_details('INFO', "CURL response validation completed",
        details=response_details)
    return True, ""

def execute_curl(curl_command, retries=3, retry_delay=1, clean_before=False, validate_output=False, timeout=3600):
    """Execute a CURL command with retries and improved error handling"""
    execution_details = {
        'command': curl_command,
        'retries_configured': retries,
        'retry_delay': retry_delay,
        'clean_before': clean_before,
        'validate_output': validate_output,
        'timeout': timeout,
        'attempts': []
    }

    if not ensure_directory_writable():
        log_with_details('ERROR', "Failed to verify directory is writable",
            details=execution_details)
        return False, "", "Failed to verify directory is writable"

    if clean_before:
        cleanup_existing_mp4s()

    for attempt in range(retries):
        attempt_details = {
            'attempt_number': attempt + 1,
            'start_time': datetime.now().isoformat()
        }
        
        log_with_details('INFO', f"Executing CURL command (attempt {attempt + 1}/{retries})",
            details={'current_attempt': attempt_details, **execution_details})
        
        try:
            process = subprocess.Popen(
                curl_command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )

            try:
                stdout, stderr = process.communicate(timeout=timeout)
                stdout_str = stdout.decode(errors='replace')
                stderr_str = stderr.decode(errors='replace')
                
                attempt_details.update({
                    'stdout_preview': stdout_str[:1000] + ('...' if len(stdout_str) > 1000 else ''),
                    'stderr_preview': stderr_str[:1000] + ('...' if len(stderr_str) > 1000 else ''),
                    'return_code': process.returncode
                })
                
            except subprocess.TimeoutExpired:
                process.kill()
                attempt_details['error'] = "Process timed out"
                execution_details['attempts'].append(attempt_details)
                log_with_details('ERROR', "Process timed out",
                    details={'failed_attempt': attempt_details, **execution_details})
                return False, "", "Process timed out"

            success, error_msg = check_curl_response(stdout_str, stderr_str)
            attempt_details['success'] = success
            attempt_details['error_message'] = error_msg
            
            if success:
                if validate_output:
                    video_file = get_latest_video()
                    if video_file:
                        is_valid, validation_msg = validate_video_file(video_file)
                        attempt_details['video_validation'] = {
                            'file': video_file,
                            'valid': is_valid,
                            'message': validation_msg
                        }
                        
                        if not is_valid:
                            if attempt < retries - 1:
                                log_with_details('ERROR', f"Video validation failed: {validation_msg}",
                                    details={'failed_attempt': attempt_details, **execution_details})
                                time.sleep(retry_delay * (2 ** attempt))
                                execution_details['attempts'].append(attempt_details)
                                continue
                            return False, stdout_str, f"Invalid video file: {validation_msg}"
                    else:
                        if attempt < retries - 1:
                            log_with_details('ERROR', "No video file found after command execution",
                                details={'failed_attempt': attempt_details, **execution_details})
                            time.sleep(retry_delay * (2 ** attempt))
                            execution_details['attempts'].append(attempt_details)
                            continue
                        return False, stdout_str, "No video file generated"

                execution_details['attempts'].append(attempt_details)
                log_with_details('INFO', "CURL command executed successfully",
                    details=execution_details)
                return True, stdout_str, stderr_str

            log_with_details('ERROR', f"Command failed: {error_msg}",
                details={'failed_attempt': attempt_details, **execution_details})
            
            if attempt < retries - 1:
                retry_delay_time = retry_delay * (2 ** attempt)
                log_with_details('INFO', f"Retrying in {retry_delay_time} seconds...",
                    details={'retry_delay': retry_delay_time, **execution_details})
                time.sleep(retry_delay_time)
                execution_details['attempts'].append(attempt_details)
                continue

        except Exception as e:
            attempt_details['error'] = str(e)
            execution_details['attempts'].append(attempt_details)
            log_with_details('ERROR', f"Error executing CURL command",
                details={'failed_attempt': attempt_details, **execution_details})
            
            if attempt < retries - 1:
                time.sleep(retry_delay * (attempt + 1))
                continue
            return False, "", f"Error: {str(e)}"

    log_with_details('ERROR', f"Failed after {retries} attempts",
        details=execution_details)
    return False, "", f"Failed after {retries} attempts"

def get_latest_video(max_retries=10, delay=2, min_size_bytes=1024):
    """Get the most recently created MP4 file in the current directory"""
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
                    try:
                        with open(video, 'rb') as f:
                            f.seek(-1, 2)
                            f.read(1)
                        valid_videos.append(video)
                    except (IOError, OSError) as e:
                        search_details.update({
                            'failed_file': video,
                            'error': str(e)
                        })
                        log_with_details('WARNING', f"Error checking video file {video}",
                            details=search_details)
                        continue
                        
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
    """Clean up a video file"""
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
    """Creates a safe temporary filename"""
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
    """Format an upload command"""
    upload_details = {
        'video_file': video_file,
        'task_data': task_data,
        'platform_data': platform_data
    }
    
    try:
        safe_video_path, original_name = create_safe_filename(video_file)
        try:
            os.rename(video_file, safe_video_path)
            upload_details['safe_video_path'] = safe_video_path
            log_with_details('INFO', f"Renamed video file for safe upload",
                details=upload_details)
        except OSError as e:
            upload_details['error'] = str(e)
            log_with_details('ERROR', f"Failed to rename video file",
                details=upload_details)
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

        upload_details.update({
            'video_title': video_title,
            'formatted_hashtags': hashtags
        })

        formatted_cmd = cmd_template.format(
            video=safe_video_path,
            description=urllib.parse.quote(video_title),
            account=urllib.parse.quote(platform_data['account_name']),
            sound=urllib.parse.quote(task_data['sound_name']),
            volume=task_data['sound_volume'],
            hashtags=urllib.parse.quote(hashtags)
        )
        
        upload_details['formatted_command'] = formatted_cmd
        log_with_details('INFO', f"Formatted upload command",
            details=upload_details)
        return formatted_cmd, safe_video_path
        
    except KeyError as e:
        upload_details['error'] = f"Missing required key: {str(e)}"
        log_with_details('ERROR', f"Missing required key in template data",
            details=upload_details)
        raise
    except Exception as e:
        upload_details['error'] = str(e)
        log_with_details('ERROR', f"Error formatting upload command",
            details=upload_details)
        raise