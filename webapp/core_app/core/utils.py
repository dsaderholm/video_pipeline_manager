import subprocess
import time
import glob
import os
import shlex
import uuid
import urllib.parse
import re
import json
import shutil
from datetime import datetime
import logging
logger = logging.getLogger('app')
import sys
from webapp.core_app.core.database import db
# Import log_manager directly here to ensure handler is activated
from webapp.core_app.core.log_manager import add_log_entry, db_log_handler

# Add the handler to the logger if not already added
if db_log_handler not in logger.handlers:
    logger.addHandler(db_log_handler)

def ensure_processed_videos_dir():
    """Ensure the processed videos directory exists"""
    videos_dir = 'processed_videos'
    # Use absolute path
    abs_videos_dir = os.path.abspath(videos_dir)
    os.makedirs(abs_videos_dir, exist_ok=True)
    return abs_videos_dir

def get_processed_video_path(task_id, schedule_time=None):
    """Get the permanent path for a processed video"""
    videos_dir = ensure_processed_videos_dir()
    timestamp = int(time.time())
    if schedule_time:
        # Include scheduled time in filename for better organization
        schedule_str = schedule_time.strftime('%Y%m%d_%H%M')
        return os.path.join(videos_dir, f'task_{task_id}_{schedule_str}_{timestamp}.mp4')
    return os.path.join(videos_dir, f'task_{task_id}_{timestamp}.mp4')

def log_with_details(level, message, task_id=None, details=None, source=None):
    """Log message with structured details to database"""
    if details is None:
        details = {}
    details['task_id'] = task_id
    
    # Only log once through the standard logger
    # This way it goes to console/file AND database via db_log_handler
    try:
        logger_level = getattr(logging, level.upper())
        log_msg = f"{message} {' (Task ' + str(task_id) + ')' if task_id else ''}"
        
        # Create a log record with all necessary details
        record = logging.LogRecord(
            name='app',
            level=logger_level,
            pathname='',
            lineno=0,
            msg=log_msg,
            args=(),
            exc_info=None
        )
        
        # Add custom attributes for the database handler
        record.task_id = task_id
        record.details = details
        record.source = source
        
        # Use logger.handle which will route through all handlers including db_log_handler
        logger.handle(record)
    except Exception as e:
        print(f"ERROR: Failed to log: {str(e)}", file=sys.stderr)
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

def check_generator_response(stdout_str, stderr_str):
    """Check if generator successfully created a video file"""
    response_details = {
        'stdout_length': len(stdout_str),
        'stderr_length': len(stderr_str),
        'stdout_sample': stdout_str[:200] if stdout_str else '',
        'stderr_sample': stderr_str[:200] if stderr_str else ''
    }
    
    # First check for HTTP 200 status code in the output
    status_match = re.search(r'HTTP/\d\.\d\s+(\d{3})', stdout_str + stderr_str)
    if status_match: 
        response_details['http_status'] = status_match.group(1)
        if status_match.group(1) == '200':
            log_with_details('INFO', "Generator returned HTTP 200 status", details=response_details)
            return True, ""
    
    # Check for successful download message in stderr
    if stderr_str and ('100' in stderr_str or 'Downloaded' in stderr_str):
        log_with_details('INFO', "Generator download appears successful", details=response_details)
        return True, ""
        
    # Also consider it a success if stderr is empty or only contains progress info
    if not stderr_str.strip() or all(
        line.startswith(('* ', '  % Total', '100', 'Warning: ')) 
        for line in stderr_str.strip().split('\n')
    ):
        log_with_details('INFO', "Generator completed with minimal output", details=response_details)
        return True, ""
        
    error_patterns = [
        r'curl:\s*\(\d+\)',
        r'Connection refused',
        r'Could not resolve host',
        r'Operation timed out',
        r'Failed to connect',
        r'404 Not Found',
        r'403 Forbidden',
        r'500 Internal Server Error'
    ]
    
    for pattern in error_patterns:
        if re.search(pattern, stderr_str + stdout_str, re.IGNORECASE):
            log_with_details('ERROR', f"Generator failed with error pattern: {pattern}", 
                            details=response_details)
            return False, stderr_str
    
    # If we don't detect specific errors, assume success
    log_with_details('INFO', "Generator considered successful (no error patterns detected)", 
                    details=response_details)
    return True, ""

def check_utility_response(stdout_str, stderr_str):
    """Check if utility successfully processed the video"""
    if not stderr_str.strip() or all(
        line.startswith(('* ', '  % Total', '100', 'Warning: ')) 
        for line in stderr_str.strip().split('\n')
    ):
        return True, ""
        
    error_patterns = [
        r'curl:\s*\(\d+\)',
        r'Connection refused',
        r'Could not resolve host',
        r'Operation timed out',
        r'Failed to connect',
        r'HTTP/[0-9.]+ 5[0-9]{2}',
        r'500 Internal Server Error'
    ]
    
    for pattern in error_patterns:
        if re.search(pattern, stderr_str + stdout_str, re.IGNORECASE):
            return False, stderr_str or stdout_str
    
    return True, ""

def check_uploader_response(stdout_str, stderr_str):
    """Check if upload was successful"""
    try:
        json_response = json.loads(stdout_str)
        if isinstance(json_response, dict):
            if 'error' in json_response or 'detail' in json_response:
                error_msg = json_response.get('error') or json_response.get('detail')
                return False, error_msg
            if 'success' in json_response or 'video_id' in json_response:
                return True, ""
    except json.JSONDecodeError:
        pass

    error_patterns = [
        r'HTTP/[0-9.]+ 5[0-9]{2}',
        r'500 Internal Server Error',
        r'The requested URL returned error: ([45]\d{2})',
        r'server error: 5\d{2}'
    ]
    
    for pattern in error_patterns:
        if re.search(pattern, stderr_str + stdout_str, re.IGNORECASE):
            return False, stdout_str or stderr_str

    return True, ""

# Fix for the utility command formatting section in execute_curl
def execute_curl(curl_command, retries=3, retry_delay=1, clean_before=False, validate_output=False, timeout=3600, mode='uploader'):
    """Enhanced curl execution with better error handling"""
    execution_details = {
        'command': curl_command,
        'attempts': [],
        'start_time': datetime.now().isoformat(),
        'mode': mode
    }
    
    # Always log when execute_curl is called
    log_with_details('INFO', f"execute_curl called with mode: {mode}", 
                     details={'command': curl_command, 'mode': mode})
    
    if clean_before:
        log_with_details('INFO', "Cleaning up existing MP4 files before execution", details={'mode': mode})
        cleanup_existing_mp4s()
    
    for attempt in range(retries):
        attempt_details = {
            'attempt': attempt + 1,
            'timestamp': datetime.now().isoformat()
        }
        
        try:
            # Fix: Ensure working directory is set to app root for consistent file paths
            cwd = os.getcwd()
            log_with_details('DEBUG', f"Current working directory: {cwd}",
                details={'command': curl_command})
            
            # For improved file path handling, just pass the command as-is
            # We'll handle all path modifications in format_upload_command
            modified_command = curl_command
            
            # Only modify commands for utility mode by directly replacing {input} with the absolute path in quotes
            if mode == 'utility':
                path_matches = re.findall(r'\{input\}', curl_command)
                if path_matches:
                    # First try to find MP4 files in the current directory
                    mp4_files = [f for f in os.listdir('.') if f.lower().endswith('.mp4')]
                    log_with_details('INFO', f"Available MP4 files for utility", 
                        details={'mp4_files': mp4_files})
                    
                    # Find the input file if it's in the command
                    file_path_matches = re.findall(r'((?:\.\/)?[\w\-\.\/\s]+\.mp4)', curl_command)
                    
                    if file_path_matches:
                        input_file = file_path_matches[0]
                        
                        # Verify file exists
                        if not os.path.exists(input_file):
                            log_with_details('WARNING', f"Input file not found at {input_file}, looking for alternatives",
                                details={'available_files': mp4_files})
                                
                            # Try to find a suitable alternative
                            if mp4_files:
                                input_file = mp4_files[0]  # Use the first available MP4
                                log_with_details('INFO', f"Using alternative input file: {input_file}")
                        
                        abs_input_file = os.path.abspath(input_file)
                        # Replace {input} with the quoted absolute path
                        modified_command = curl_command.replace('{input}', f'"{abs_input_file}"')
                        log_with_details('INFO', f"Replaced input in utility command",
                            details={'original_command': curl_command, 'modified_command': modified_command})
            

            
            # Log command details before execution
            log_with_details('INFO', f"Executing command (attempt {attempt+1}/{retries})",
                details={'original_command': curl_command, 'modified_command': modified_command})
            
            try:
                log_with_details('INFO', "Starting Popen subprocess")
                process = subprocess.Popen(
                    modified_command,
                    shell=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE
                )
                log_with_details('INFO', f"Subprocess started with PID: {process.pid}")
            except Exception as subprocess_error:
                log_with_details('ERROR', f"Failed to start subprocess: {str(subprocess_error)}",
                    details={'error': str(subprocess_error), 'command': modified_command})
                raise
            
            try:
                log_with_details('INFO', f"Waiting for subprocess to complete with timeout: {timeout}s")
                stdout, stderr = process.communicate(timeout=timeout)
                stdout_str = stdout.decode(errors='replace')
                stderr_str = stderr.decode(errors='replace')
                
                log_with_details('INFO', f"Subprocess completed with return code: {process.returncode}",
                    details={
                        'return_code': process.returncode,
                        'stdout_length': len(stdout_str),
                        'stderr_length': len(stderr_str),
                        'stdout_preview': stdout_str[:200] if stdout_str else '',
                        'stderr_preview': stderr_str[:200] if stderr_str else '',
                        'execution_time': f"{(datetime.now() - datetime.fromisoformat(execution_details['start_time'])).total_seconds():.2f}s"
                    })
            except subprocess.TimeoutExpired as timeout_error:
                log_with_details('ERROR', f"Subprocess timed out after {timeout}s",
                    details={
                        'timeout': timeout,
                        'pid': process.pid,
                        'command': modified_command
                    })
                # Try to kill the process
                try:
                    process.kill()
                    log_with_details('INFO', f"Killed timed out process (PID: {process.pid})")
                except Exception as kill_error:
                    log_with_details('WARNING', f"Failed to kill timed out process: {str(kill_error)}")
                raise
            
            attempt_details.update({
                'return_code': process.returncode,
                'stdout_length': len(stdout_str),
                'stderr_length': len(stderr_str)
            })

            # Parse the Content-Disposition header for the filename when in generator mode
            generated_filename = None
            if mode == 'generator':
                cd_match = re.search(r'Content-Disposition:.*filename="?([^";\n]+)', stderr_str, re.IGNORECASE)
                if cd_match:
                    generated_filename = cd_match.group(1)
                    attempt_details['generated_filename'] = generated_filename
                    log_with_details('INFO', f"Found filename in Content-Disposition header: {generated_filename}",
                        details={'stdout_length': len(stdout_str), 'stderr_length': len(stderr_str)})
                else:
                    log_with_details('WARNING', "No filename found in Content-Disposition header",
                        details={'stderr_snippet': stderr_str[:500]})
            
            # Use appropriate response checker based on mode
            if mode == 'generator':
                success, error_msg = check_generator_response(stdout_str, stderr_str)
                # Explicitly log if we got HTTP 200
                if re.search(r'HTTP/\d\.\d\s+200', stdout_str + stderr_str):
                    log_with_details('INFO', "Generator returned HTTP 200 status",
                        details={'stdout_snippet': stdout_str[:100], 'stderr_snippet': stderr_str[:100]})
            elif mode == 'utility':
                success, error_msg = check_utility_response(stdout_str, stderr_str)
            else:  # uploader mode
                success, error_msg = check_uploader_response(stdout_str, stderr_str)
                
            attempt_details['success'] = success
            attempt_details['error_message'] = error_msg
            
            if success:
                if validate_output and mode == 'generator':
                    # List files in current directory to help with debugging
                    try:
                        files = os.listdir('.')
                        mp4_files = [f for f in files if f.lower().endswith('.mp4')]
                        log_with_details('INFO', f"Files in directory after generator execution", 
                            details={'all_files': files, 'mp4_files': mp4_files})
                    except Exception as e:
                        log_with_details('WARNING', f"Error listing directory contents: {str(e)}", 
                            details={'error': str(e)})
                        
                    # Try to find the video file
                    video_file = get_latest_video()
                    if not video_file:
                        if attempt < retries - 1:
                            log_with_details('WARNING', "No video file found, retrying...",
                                details=attempt_details)
                            time.sleep(retry_delay * (2 ** attempt))
                            continue
                        return False, stdout_str, "No video file generated"
                    
                    log_with_details('INFO', f"Found video file: {video_file}",
                        details={'video_file': video_file})
                    
                    is_valid, validation_msg = validate_video_file(video_file)
                    if not is_valid:
                        if attempt < retries - 1:
                            log_with_details('WARNING', f"Invalid video file: {validation_msg}, retrying...",
                                details=attempt_details)
                            time.sleep(retry_delay * (2 ** attempt))
                            continue
                        return False, stdout_str, f"Invalid video file: {validation_msg}"
                
                execution_details['attempts'].append(attempt_details)
                log_with_details('INFO', "Curl command executed successfully", details=execution_details)
                return True, stdout_str, generated_filename if mode == 'generator' else stderr_str
            
            if attempt < retries - 1:
                retry_delay_time = retry_delay * (2 ** attempt)
                log_with_details('WARNING', f"Command failed, retrying in {retry_delay_time} seconds",
                    details={'error': error_msg, 'attempt': attempt + 1, 'max_retries': retries})
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
    # Ensure file_path is absolute
    abs_file_path = os.path.abspath(file_path)
    
    validation_details = {
        'file_path': abs_file_path,
        'original_path': file_path,
        'min_size_bytes': min_size_bytes,
        'checks': [],
        'is_processed': 'processed_videos' in abs_file_path
    }
    
    def add_check(name, result, message=None):
        validation_details['checks'].append({
            'name': name,
            'result': result,
            'message': message
        })
        return result
    
    if not add_check('file_exists', os.path.exists(abs_file_path)):
        log_with_details('ERROR', "Video file validation failed: File does not exist",
            details=validation_details)
        return False, "File does not exist"
    
    file_size = os.path.getsize(abs_file_path)
    validation_details['file_size'] = file_size
    
    if not add_check('size_check', file_size >= min_size_bytes):
        log_with_details('ERROR', "Video file validation failed: File too small",
            details=validation_details)
        return False, f"File too small (< {min_size_bytes} bytes)"
    
    try:
        with open(abs_file_path, 'rb') as f:
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
            
            # Create a temporary file with a safe name for ffprobe
            temp_dir = os.path.dirname(abs_file_path) or '.'
            temp_name = f"temp_validate_{uuid.uuid4().hex[:8]}.mp4"
            temp_path = os.path.join(temp_dir, temp_name)
            
            try:
                # Copy the file with a safe name
                shutil.copy2(abs_file_path, temp_path)
                validation_details['temp_path'] = temp_path
                
                # Run ffprobe on the temp file
                try:
                    result = subprocess.run(
                        ['ffprobe', '-v', 'error', '-i', temp_path],
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
                    
            finally:
                # Clean up temp file
                try:
                    if os.path.exists(temp_path):
                        os.remove(temp_path)
                except Exception as e:
                    log_with_details('WARNING', f"Failed to remove temporary validation file: {str(e)}",
                        details={'temp_path': temp_path, 'error': str(e)})
            
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
        # Get current working directory for better logging
        cwd = os.getcwd()
        cleanup_details['current_dir'] = cwd
        log_with_details('INFO', f"Cleaning up temporary MP4 files in {cwd}")
        
        # First try with glob pattern
        mp4_files = glob.glob("*.mp4")
        
        # Also try direct directory listing which might be more reliable
        try:
            for file in os.listdir(cwd):
                if file.lower().endswith('.mp4') and file not in mp4_files:
                    mp4_files.append(file)
        except Exception as dir_error:
            cleanup_details['dir_listing_error'] = str(dir_error)
            log_with_details('WARNING', f"Error in directory listing: {str(dir_error)}",
                details={'dir': cwd, 'error': str(dir_error)})
        
        # Log what we found
        cleanup_details['files_found'] = mp4_files
        log_with_details('INFO', f"Found {len(mp4_files)} MP4 files to clean up",
            details={'files': mp4_files})
        
        # Clean up temporary MP4 files (not in processed_videos)
        for file in mp4_files:
            # Skip files in processed_videos directory
            if 'processed_videos' in file:
                log_with_details('INFO', f"Skipping processed video file: {file}")
                continue
                
            # Get absolute path for better handling
            abs_path = os.path.abspath(os.path.join(cwd, file))
            
            try:
                if os.path.exists(abs_path):
                    # Get file size for logging
                    try:
                        file_size = os.path.getsize(abs_path)
                    except:
                        file_size = -1
                        
                    # Use multiple attempts if needed
                    removed = False
                    for attempt in range(3):
                        try:
                            os.remove(abs_path)
                            removed = True
                            break
                        except Exception as retry_error:
                            # Sleep briefly before retry
                            time.sleep(0.5)
                    
                    if removed:
                        cleanup_details['files_removed'].append(file)
                        log_with_details('INFO', f"Cleaned up existing file: {file}",
                            details={'file': abs_path, 'size': file_size})
                    else:
                        error_detail = {'file': abs_path, 'error': 'Failed after multiple attempts'}
                        cleanup_details['errors'].append(error_detail)
                        log_with_details('WARNING', f"Could not remove file after multiple attempts: {file}",
                            details=error_detail)
            except Exception as e:
                error_detail = {'file': abs_path, 'error': str(e)}
                cleanup_details['errors'].append(error_detail)
                log_with_details('WARNING', f"Could not remove file {file}",
                    details=error_detail)
                
        if cleanup_details['files_removed']:
            log_with_details('INFO', f"Cleanup completed successfully: removed {len(cleanup_details['files_removed'])} files",
                details=cleanup_details)
        else:
            log_with_details('INFO', "No files needed to be removed",
                details=cleanup_details)
                
    except Exception as e:
        cleanup_details['error'] = str(e)
        log_with_details('ERROR', f"Error during cleanup: {str(e)}",
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
    
    log_with_details('INFO', f"Starting search for video files with {max_retries} attempts",
                    details=search_details)
    
    # Track all files we've seen through all attempts for better debugging
    all_seen_files = []
    
    for attempt in range(max_retries):
        # Search in multiple locations for better compatibility with Docker
        video_files = []
        
        # Look for all MP4 files in the root directory (simpler approach)
        try:
            current_dir = os.getcwd()
            for file in os.listdir(current_dir):
                if file.lower().endswith('.mp4'):
                    abs_path = os.path.abspath(os.path.join(current_dir, file))
                    video_files.append(abs_path)
                    if abs_path not in all_seen_files:
                        all_seen_files.append(abs_path)
        except Exception as e:
            log_with_details('WARNING', f"Error listing current directory: {str(e)}",
                            details={'error': str(e), 'current_dir': current_dir})
        
        # Also search in processed_videos directory
        try:
            proc_dir = os.path.join(current_dir, 'processed_videos')
            if os.path.exists(proc_dir) and os.path.isdir(proc_dir):
                for file in os.listdir(proc_dir):
                    if file.lower().endswith('.mp4'):
                        abs_path = os.path.abspath(os.path.join(proc_dir, file))
                        video_files.append(abs_path)
                        if abs_path not in all_seen_files:
                            all_seen_files.append(abs_path)
        except Exception as e:
            log_with_details('WARNING', f"Error listing processed_videos directory: {str(e)}",
                            details={'error': str(e), 'proc_dir': proc_dir})
        
        # Log what we found
        log_with_details('INFO', f"Searching for video files (attempt {attempt + 1}/{max_retries})",
            details={'found_files': len(video_files), 'files': video_files})
        
        # Filter files by size and make sure they're readable
        valid_videos = []
        for video in video_files:
            try:
                if os.path.exists(video) and os.path.isfile(video):
                    size = os.path.getsize(video)
                    if size >= min_size_bytes:
                        # Quick check that the file is readable
                        with open(video, 'rb') as f:
                            header = f.read(8)  # Read first 8 bytes
                        valid_videos.append(video)
                        log_with_details('INFO', f"Validated video file: {video}",
                                        details={'file_size': size})
                    else:
                        log_with_details('WARNING', f"File too small: {video}",
                                        details={'file_size': size, 'min_size': min_size_bytes})
                else:
                    log_with_details('WARNING', f"File doesn't exist or is not a file: {video}")
            except Exception as e:
                log_with_details('WARNING', f"Error validating file {video}: {str(e)}",
                                details={'error': str(e)})
                
        if valid_videos:
            try:
                # Just use the most recent file
                latest_video = max(valid_videos, key=os.path.getctime)
                creation_time = datetime.fromtimestamp(os.path.getctime(latest_video))
                log_with_details('INFO', f"Found valid video file: {latest_video}",
                                details={'creation_time': creation_time.isoformat()})
                return latest_video
            except Exception as e:
                log_with_details('ERROR', f"Error getting most recent file: {str(e)}",
                                details={'error': str(e), 'valid_videos': valid_videos})
            
        # No valid videos found, sleep and try again
        if attempt < max_retries - 1:
            sleep_time = delay * (attempt + 1)  # Increasing backoff
            log_with_details('INFO', f"No valid videos found, retrying in {sleep_time} seconds (attempt {attempt + 1}/{max_retries})")
            time.sleep(sleep_time)
    
    # Final attempt failed
    log_with_details('WARNING', f"Failed to find any valid video files after {max_retries} attempts",
                    details={'all_seen_files': all_seen_files})
    return None

def cleanup_video(video_file):
    """Clean up a video file with improved error handling"""
    if not video_file:
        return

    cleanup_details = {
        'video_file': video_file,
        'exists': False,
        'size': None,
        'success': False,
        'is_processed': video_file.startswith('processed_videos/')
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
                    # If it's a processed video and not marked for deletion, keep it
                    if cleanup_details['is_processed']:
                        cleanup_details['skipped'] = True
                        log_with_details('INFO', f"Skipping processed video file",
                            details=cleanup_details)
                        break
                    
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

def cleanup_processed_video(video_path):
    """Clean up a processed video while maintaining the directory"""
    if not video_path or not os.path.exists(video_path):
        return
        
    cleanup_details = {
        'video_path': video_path,
        'is_processed': video_path.startswith('processed_videos/'),
        'exists': os.path.exists(video_path)
    }
    
    try:
        # Only remove if it's in the processed_videos directory
        if cleanup_details['is_processed']:
            os.remove(video_path)
            cleanup_details['success'] = True
            log_with_details('INFO', "Cleaned up processed video",
                details=cleanup_details)
    except Exception as e:
        cleanup_details['error'] = str(e)
        log_with_details('ERROR', f"Failed to clean up processed video: {str(e)}",
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
        'safe_path': safe_path,
        'is_processed': original_path.startswith('processed_videos/')
    }
    
    log_with_details('INFO', f"Created safe filename",
        details=filename_details)
    return safe_path, original_name

# This is the problematic function in utils.py that needs to be modified
def format_upload_command(cmd_template, video_file, task_data, platform_data):
    """Format an upload command with improved error handling and validation"""
    upload_details = {
        'video_file': video_file,
        'task_id': task_data.get('id'),
        'platform': platform_data.get('name', 'unknown')
    }
    
    try:
        # Use absolute path for video file
        abs_video_file = os.path.abspath(video_file)
        safe_video_path, _ = create_safe_filename(abs_video_file)
        try:
            # For processed videos, make a copy instead of moving
            if 'processed_videos' in abs_video_file:
                shutil.copy2(abs_video_file, safe_video_path)
            else:
                # Try copying instead of renaming for better reliability
                shutil.copy2(abs_video_file, safe_video_path)
                try:
                    os.remove(abs_video_file)
                except:
                    pass  # Ignore errors on removal
        except OSError as e:
            log_with_details('ERROR', f"Failed to prepare video file: {str(e)}", 
                details={'video_file': video_file, 'abs_path': abs_video_file, 'safe_path': safe_video_path, 'error': str(e)})
            return None, None

        # THE FIX: Prioritize the original name from the database
        # In process_video_upload, task_data already has original_name from the database
        # This preserves the original name fetched from the generator
        original_name = task_data.get('original_name', "").strip()

        # If the original name is missing, log a warning and use the filename instead
        if not original_name:
            log_with_details('WARNING', "Original name is missing, falling back to filename",
                            details={'task_id': task_data.get('id'), 'video_file': video_file})
            original_name = os.path.basename(video_file)

        # Remove extension for the description/title
        video_title = os.path.splitext(original_name)[0]

        log_with_details('INFO', f"Using original video title for description: {video_title}",
            task_id=task_data.get('id'),
            details={'original_name': original_name, 'video_title': video_title, 'from_path': video_file})
    
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

        # Format the command with all required parameters
        formatted_cmd = cmd_template.format(
            video=f'{safe_video_path}',  # Use quotes to handle spaces in path
            description=video_title,
            account=platform_data['account_name'],
            sound=task_data['sound_name'],
            volume=task_data['sound_volume'],
            hashtags=hashtags,
            input=f'"{safe_video_path}"'  # Also handle the input parameter for utilities
        )
        
        upload_details.update({
            'safe_video_path': safe_video_path,
            'video_title': video_title,
            'formatted_command': formatted_cmd
        })
        
        log_with_details('INFO', "Successfully formatted upload command", 
            details=upload_details)
        
        return formatted_cmd, safe_video_path
        
    except Exception as e:
        upload_details['error'] = str(e)
        log_with_details('ERROR', f"Error formatting upload command: {str(e)}", 
            details=upload_details)
        raise