                # Special handling for utilities that return non-zero codes
                if mode == 'utility' and process.returncode != 0:
                    # Check if this might be a successful result despite the return code
                    # For example, if using a command that writes back to the input file
                    is_likely_success = False
                    
                    # These patterns often indicate the command worked but return code is non-zero
                    success_despite_error_patterns = [
                        # File successfully written but connection closed
                        'Connection #0 to host',
                        # URLs that modify files in place are likely to be successful even with errors
                        'output="{input}"',
                        '--output "{input}"'
                    ]
                    
                    # Check if the command template or command line includes patterns that suggest
                    # the file was successfully modified even with a non-zero return code
                    for pattern in success_despite_error_patterns:
                        if pattern in modified_command or pattern in curl_command:
                            is_likely_success = True
                            log_with_details('INFO', f"Utility command likely succeeded despite return code {process.returncode}",
                                details={'reason': pattern, 'returncode': process.returncode})
                            break
                    
                    # Don't write to stderr if no actual error occurred
                    if is_likely_success and not re.search(r'(error|fail|exception)', stderr_str.lower()):
                        stdout, stderr = stdout, stderr  # Keep original values
                        # But set returncode to 0 for success handling
                        process.returncode = 0
                        log_with_details('INFO', "Treating utility as successful despite non-zero return code",
                            details={'original_return_code': process.returncode, 'stderr': stderr_str[:200] if stderr_str else ''})    # Check if a return code of 2 is being treated as a failure despite success
    # This is a common issue with some curl utilities that return non-zero exit codes
    # when they actually processed the file successfully
    if stderr_str and not re.search(r'(error|failed|failure)', stderr_str.lower()):
        # Look for common patterns indicating the file was successfully processed
        if 'output="{input}"' in cmd_template or '--output "{input}"' in cmd_template:
            log_with_details('INFO', "Utility uses input file as output, treating as successful",
                details={'cmd_template': cmd_template})
            success = True    # YouTube-specific errors
    youtube_error_patterns = [
        r'invalid_grant',
        r'token (has been )?expired',
        r'token (has been )?revoked',
        r'authentication (required|failed)',
        r'auth(entication|orization) error',
        r'quota exceeded',
        r'daily limit',
        r'invalid credentials',
        r'access (not )?authorized'
    ]
    
    for pattern in youtube_error_patterns:
        if re.search(pattern, full_response, re.IGNORECASE):
            match = re.search(pattern, full_response, re.IGNORECASE)
            error_msg = f"YouTube error: {match.group(0) if match else pattern}"
            error_details = "Token has been expired or revoked. Please refresh your YouTube access token."
            
            log_with_details('ERROR', f"YouTube API error: {error_msg}", 
                details={
                    'error_type': 'youtube_api',
                    'match': match.group(0) if match else pattern,
                    'recommendation': 'Refresh YouTube API token'
                })
            return False, f"Error uploading to YouTube: {error_details}"import subprocess
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
    """Check if utility successfully processed the video with improved debugging"""
    # Log the complete output for better debugging
    log_with_details('INFO', f"Utility response received",
        details={
            'stdout_length': len(stdout_str),
            'stderr_length': len(stderr_str),
            'stdout_preview': stdout_str[:500] if stdout_str else '',
            'stderr_preview': stderr_str[:500] if stderr_str else ''
        })
    
    # Check if stderr is empty or contains only progress info
    if not stderr_str.strip() or all(
        line.startswith(('* ', '  % Total', '100', 'Warning: ')) 
        for line in stderr_str.strip().split('\n')
    ):
        # Look for HTTP 200 in stdout - this indicates success
        if 'HTTP/1.1 200' in stdout_str or 'HTTP/2 200' in stdout_str:
            log_with_details('INFO', "Utility returned HTTP 200 status",
                details={'success': True})
            return True, ""
            
        # Check for successful download indicators
        if 'Downloaded' in stderr_str or '100 ' in stderr_str:
            log_with_details('INFO', "Utility download appears successful",
                details={'success': True})
            return True, ""
            
        # If stdout has reasonable length but no error patterns, assume success
        if len(stdout_str) > 100:
            log_with_details('INFO', "Utility produced substantial output, assuming success",
                details={'success': True})
            return True, ""
            
    # Check for commands that might modify files in place
    if '--output' in stdout_str + stderr_str or 'output=' in stdout_str + stderr_str:
        log_with_details('INFO', "Command likely modifies file in place, treating as success")
        return True, ""
    
    # Look for specific error patterns
    error_patterns = [
        r'curl:\s*\(\d+\)',
        r'Connection refused',
        r'Could not resolve host',
        r'Operation timed out',
        r'Failed to connect',
        r'HTTP/[0-9.]+ (4[0-9]{2}|5[0-9]{2})',  # Include 4xx errors too
        r'500 Internal Server Error',
        r'404 Not Found',
        r'401 Unauthorized',
        r'403 Forbidden'
    ]
    
    for pattern in error_patterns:
        match = re.search(pattern, stderr_str + stdout_str, re.IGNORECASE)
        if match:
            error_msg = match.group(0)
            log_with_details('ERROR', f"Utility failed with error: {error_msg}",
                details={
                    'error_pattern': pattern,
                    'error_match': error_msg,
                    'stderr': stderr_str[:500] if stderr_str else ''
                })
            return False, stderr_str or stdout_str
    
    # If no specific errors found but stderr has content, log it as a warning
    if stderr_str.strip() and not stderr_str.startswith(('* ', '  % Total', '100', 'Warning: ')):
        log_with_details('WARNING', "Utility produced stderr output but no recognized error pattern",
            details={'stderr': stderr_str[:500]})
    
    # Default to success if no clear error detected
    return True, ""

def check_uploader_response(stdout_str, stderr_str):
    """Check if upload was successful with comprehensive error detection"""
    # Log full response details for debugging
    response_details = {
        'stdout_length': len(stdout_str),
        'stderr_length': len(stderr_str),
        'stdout_preview': stdout_str[:500] if stdout_str else '',
        'stderr_preview': stderr_str[:500] if stderr_str else ''
    }
    log_with_details('INFO', "Checking uploader response", 
                   details=response_details)
    
    # Combine stdout and stderr for easier searching
    full_response = stdout_str + stderr_str
    
    # First check for HTTP 500 errors explicitly - these are critical to catch
    http_500_patterns = [
        r'HTTP/[0-9\.]+ 500',
        r'500 Internal Server Error',
        r'The requested URL returned error: 500',
        r'server error: 500'
    ]
    
    for pattern in http_500_patterns:
        if re.search(pattern, full_response, re.IGNORECASE):
            error_msg = f"HTTP 500 Server Error detected: {pattern}"
            log_with_details('ERROR', error_msg, 
                details={'error_type': 'server_error', 'match': pattern})
            return False, error_msg
    
    # Check for all HTTP 4xx and 5xx errors
    http_error_match = re.search(r'HTTP/[0-9\.]+ ([45][0-9]{2})', full_response, re.IGNORECASE)
    if http_error_match:
        status_code = http_error_match.group(1)
        error_msg = f"HTTP error {status_code}"
        log_with_details('ERROR', error_msg, 
            details={'error_type': 'http_error', 'status_code': status_code})
        return False, error_msg
    
    # General authentication/login errors
    auth_error_patterns = [
        r'NO COOKIES FILE FOUND',
        r'COOKIES EXPIRED',
        r'PLEASE LOG-IN',
        r'LOGIN (REQUIRED|FAILURE)',
        r'authentication failed',
        r'login (error|failed)',
        r'rate limited',
        r'token expired',
        r'not authorized',
        r'video (upload|processing) failed'
    ]
    
    for pattern in auth_error_patterns:
        if re.search(pattern, full_response, re.IGNORECASE):
            error_msg = f"Authentication error: {pattern}"
            log_with_details('ERROR', error_msg, 
                details={'error_type': 'auth', 'match': pattern})
            return False, error_msg
    
    # General connection errors
    connection_error_patterns = [
        r'curl: \((\d+)\)',
        r'Connection refused',
        r'Could not resolve host',
        r'Operation timed out',
        r'Failed to connect',
        r'SSL (certificate|handshake) (error|problem)',
        r'connection reset by peer'
    ]
    
    for pattern in connection_error_patterns:
        match = re.search(pattern, full_response, re.IGNORECASE)
        if match:
            error_detail = match.group(0)
            error_msg = f"Connection error: {error_detail}"
            log_with_details('ERROR', error_msg, 
                details={'error_type': 'connection', 'match': error_detail})
            return False, error_msg
    
    # Check for JSON error responses
    try:
        json_start = stdout_str.find('{')
        if json_start >= 0:
            json_response = json.loads(stdout_str[json_start:])
            if isinstance(json_response, dict):
                # Check for error indicators
                if any(key in json_response for key in ['error', 'errors', 'detail', 'message']) and \
                   not (json_response.get('success') == True or 'video_id' in json_response):
                    
                    error_key = next((k for k in ['error', 'errors', 'detail', 'message'] if k in json_response), None)
                    error_msg = f"API error: {json_response.get(error_key)}"
                    log_with_details('ERROR', error_msg, 
                        details={'error_type': 'api', 'json_response': json_response})
                    return False, error_msg
                
                # Check for success indicators
                if json_response.get('success') == True or \
                   any(key in json_response for key in ['video_id', 'id', 'media_id']):
                    log_with_details('INFO', "Upload successful based on JSON response", 
                        details={'success_indicators': [k for k in ['success', 'video_id', 'id', 'media_id'] if k in json_response]})
                    return True, ""
    except Exception as e:
        # JSON parsing error is not a failure
        log_with_details('INFO', f"JSON parsing error (not critical): {str(e)}", 
            details={'error': str(e)})
    
    # Check for success patterns in text output
    success_patterns = [
        r'upload(ed)? successful',
        r'100% complete',
        r'video (_)?id',
        r'media (_)?id',
        r'success',
        r'HTTP/[0-9\.]+ 20[0-9]'
    ]
    
    for pattern in success_patterns:
        if re.search(pattern, full_response, re.IGNORECASE):
            success_match = re.search(pattern, full_response, re.IGNORECASE).group(0)
            log_with_details('INFO', f"Upload successful: {success_match}", 
                details={'match': success_match})
            return True, ""
    
    # If stderr is empty, it's likely a success
    if not stderr_str.strip() and len(stdout_str) > 0:
        log_with_details('INFO', "Upload appears successful (no error output)", 
            details={'stdout_length': len(stdout_str)})
        return True, ""
    
    # If stderr only contains progress indicators or curl info, it's likely a success
    if stderr_str and all(line.startswith(('* ', '  % Total', '100')) 
                    for line in stderr_str.strip().split('\n')):
        log_with_details('INFO', "Upload appears successful (only progress indicators in stderr)", 
            details={'stderr_preview': stderr_str[:200]})
        return True, ""
        
    # If process returned successful but we can't confirm it, log a warning but treat as success
    if not stderr_str.strip():
        log_with_details('WARNING', "Upload treated as successful but without clear confirmation", 
            details={'stdout_preview': stdout_str[:200]})
        return True, ""
    
    # If stderr contains substantial output but no recognized errors, log as a warning
    log_with_details('WARNING', "Upload has stderr output but no recognized error patterns", 
        details={'stderr_preview': stderr_str[:300]})
        
    # Default to failure if stderr is not empty and no success patterns found
    if stderr_str.strip():
        error_msg = "Upload failed with unrecognized error"
        log_with_details('ERROR', error_msg, 
            details={'stderr_preview': stderr_str[:300]})
        return False, stderr_str
    
    # If we get here with no clear indicators either way, default to success
    return True, ""

# Fix for the utility command formatting section in execute_curl
def execute_curl(curl_command, retries=3, retry_delay=1, clean_before=False, validate_output=False, timeout=3600, mode='uploader'):
    """Execute curl command with reliable, generic error detection for all service types.
    
    This function handles executing curl commands for generators, utilities, and uploaders
    with flexible, category-based error detection that doesn't rely on special cases for
    specific applications.
    """
    execution_details = {
        'command': curl_command,
        'attempts': [],
        'start_time': datetime.now().isoformat(),
        'mode': mode,
        'clean_before': clean_before,
        'validate_output': validate_output,
        'timeout': timeout
    }
    
    # Always log when execute_curl is called
    log_with_details('INFO', f"execute_curl called with mode: {mode}", 
                     details={'command': curl_command, 'mode': mode})
    
    if clean_before:
        log_with_details('INFO', "Cleaning up existing MP4 files before execution")
        cleanup_existing_mp4s()
    
    for attempt in range(retries):
        attempt_details = {
            'attempt': attempt + 1,
            'timestamp': datetime.now().isoformat()
        }
        
        try:
            # Ensure working directory is set to app root for consistent file paths
            cwd = os.getcwd()
            
            # Default to using command as-is
            modified_command = curl_command
            
            # Handle {input} placeholder for utility commands
            if mode == 'utility':
                log_with_details('INFO', "Processing utility command", 
                    details={'original_command': curl_command})
                
                # Find MP4 files in common locations
                mp4_files = []
                search_paths = ['.', '/tmp', '/var/tmp']
                processed_dir = os.path.join(cwd, 'processed_videos')
                if os.path.exists(processed_dir):
                    search_paths.append(processed_dir)
                    
                log_with_details('INFO', "Searching for MP4 files in multiple paths", 
                    details={'search_paths': search_paths})
                                
                # Search for MP4 files in all paths
                for search_path in search_paths:
                    if os.path.exists(search_path):
                        try:
                            for file in os.listdir(search_path):
                                if file.lower().endswith('.mp4'):
                                    if os.path.isabs(search_path):
                                        file_path = os.path.join(search_path, file)
                                    else:
                                        file_path = os.path.abspath(os.path.join(cwd, search_path, file))
                                    
                                    # Check file size to ensure it's a valid file
                                    try:
                                        size = os.path.getsize(file_path)
                                        if size >= 1024:  # At least 1KB
                                            mp4_files.append(file_path)
                                    except Exception as size_error:
                                        log_with_details('WARNING', f"Error checking file size: {str(size_error)}")
                        except Exception as list_error:
                            log_with_details('WARNING', f"Error listing directory {search_path}: {str(list_error)}")
                
                log_with_details('INFO', f"Found MP4 files for utility", 
                    details={'mp4_files': mp4_files})
                
                if mp4_files:
                    # Sort by creation time to get the most recent file
                    try:
                        mp4_files.sort(key=os.path.getctime, reverse=True)
                    except Exception as sort_error:
                        log_with_details('WARNING', f"Error sorting files by creation time: {str(sort_error)}")
                    
                    input_file = mp4_files[0]  # Use the most recent MP4 file
                    
                    # Docker uses forward slashes for paths
                    input_file = input_file
                    
                    # Replace {input} with the path
                    modified_command = curl_command.replace('{input}', input_file)
                    log_with_details('INFO', "Replaced input in utility command",
                        details={
                            'original_command': curl_command, 
                            'modified_command': modified_command,
                            'input_file': input_file
                        })
                else:
                    log_with_details('ERROR', "No MP4 files found for utility command")
                    # Return failure immediately if no MP4 files found
                    return False, "", "No MP4 files found for processing"
            
            # Log command details before execution
            log_with_details('INFO', f"Executing command (attempt {attempt+1}/{retries})")
            
            try:
                log_with_details('INFO', "Starting Popen subprocess")
                
                # Fix environment path issues by making sure all paths use forward slashes
                fixed_command = modified_command
                
                # Check if this is a command for a network service
                if 'http://' in fixed_command:
                    # Extract the IP and port from the command
                    service_match = re.search(r'http://([0-9.]+):([0-9]+)', fixed_command)
                    if service_match:
                        service_ip = service_match.group(1)
                        service_port = service_match.group(2)
                        log_with_details('INFO', f"Detected microservice call to {service_ip}:{service_port}")
                        
                        # Verify the service is available before attempting the call
                        try:
                            import socket
                            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                            s.settimeout(5)  # 5 second timeout
                            connect_result = s.connect_ex((service_ip, int(service_port)))
                            s.close()
                            
                            if connect_result == 0:
                                log_with_details('INFO', f"Successfully connected to service at {service_ip}:{service_port}")
                            else:
                                log_with_details('WARNING', f"Cannot connect to service at {service_ip}:{service_port} (error: {connect_result})")
                                    
                                # Try to add a timeout parameter to curl if not already present
                                if '--connect-timeout' not in fixed_command:
                                    fixed_command = fixed_command.replace('curl', 'curl --connect-timeout 10', 1)
                                    log_with_details('INFO', "Added connection timeout to curl command")
                        except Exception as socket_err:
                            log_with_details('WARNING', f"Socket test failed: {str(socket_err)}")
                
                # Setup environment with correct paths
                env = os.environ.copy()
                # If we're in Docker, make sure PATH includes needed directories
                if os.path.exists('/usr/local/bin/curl'):
                    env['PATH'] = f"/usr/local/bin:/usr/bin:/bin:{env.get('PATH', '')}" 
                # Docker-only environment - curl should be available in the container
                
                # Ensure working directory exists (Docker compatibility)
                working_dir = os.getcwd()
                os.makedirs(working_dir, exist_ok=True)
                
                log_with_details('INFO', "Starting process with env")
                
                # Run the command
                process = subprocess.Popen(
                    fixed_command,
                    shell=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                    cwd=working_dir
                )
                log_with_details('INFO', f"Subprocess started with PID: {process.pid}")
            except Exception as subprocess_error:
                log_with_details('ERROR', f"Failed to start subprocess: {str(subprocess_error)}")
                raise
            
            try:
                log_with_details('INFO', f"Waiting for subprocess to complete with timeout: {timeout}s")
                stdout, stderr = process.communicate(timeout=timeout)
                stdout_str = stdout.decode(errors='replace')
                stderr_str = stderr.decode(errors='replace')
                
                log_with_details('INFO', f"Subprocess completed with return code: {process.returncode}")
                
                # Save logs to a backup log file for debugging
                if mode in ['generator', 'utility']:
                    try:
                        logs_dir = os.path.join(os.getcwd(), 'backup_logs')
                        os.makedirs(logs_dir, exist_ok=True)
                        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                        log_file = os.path.join(logs_dir, f"{mode}_{timestamp}.log")
                        with open(log_file, 'w', encoding='utf-8') as f:
                            f.write(f"Command: {fixed_command}\n")
                            f.write(f"Return code: {process.returncode}\n")
                            f.write(f"\nSTDOUT:\n{stdout_str}\n")
                            f.write(f"\nSTDERR:\n{stderr_str}\n")
                        log_with_details('INFO', f"Saved {mode} logs to {log_file}")
                    except Exception as log_error:
                        log_with_details('WARNING', f"Failed to save backup logs: {str(log_error)}")
                
            except subprocess.TimeoutExpired as timeout_error:
                log_with_details('ERROR', f"Subprocess timed out after {timeout}s")
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
                cd_match = re.search(r'Content-Disposition:.*filename="?([^";
]+)', stderr_str, re.IGNORECASE)
                if cd_match:
                    generated_filename = cd_match.group(1)
                    attempt_details['generated_filename'] = generated_filename
                    log_with_details('INFO', f"Found filename in Content-Disposition header: {generated_filename}")
                else:
                    log_with_details('WARNING', "No filename found in Content-Disposition header")
            
            # ===== SIMPLIFIED ERROR DETECTION BASED ON HTTP STATUS AND RETURN CODES =====
            # This is the main focus of our changes - making error detection reliable
            # while avoiding special cases for specific services
            
            # Combine stdout and stderr for searching (HTTP status codes could be in either)
            full_response = stdout_str + stderr_str
            is_success = False
            error_message = ""
            
            # 1. Check for HTTP status codes (strongest indicator of success/failure)
            http_status_match = re.search(r'HTTP/[0-9.]+ (\d{3})', full_response)
            if http_status_match:
                status_code = int(http_status_match.group(1))
                if 200 <= status_code < 300:
                    # 2xx status codes always indicate success
                    log_with_details('INFO', f"HTTP {status_code} status code indicates success")
                    is_success = True
                elif status_code >= 400:
                    # 4xx/5xx status codes always indicate failure
                    error_message = f"HTTP error {status_code}"
                    log_with_details('ERROR', error_message)
                    is_success = False
                    
                    # For HTTP errors, retry if we have attempts left
                    if attempt < retries - 1:
                        retry_timeout = retry_delay * (2 ** attempt)
                        log_with_details('INFO', f"Retrying in {retry_timeout} seconds")
                        time.sleep(retry_timeout)
                        continue
                    return False, stdout_str, error_message
            
            # 2. Check for common network/curl errors in output
            error_patterns = [
                r'curl:\s*\(\d+\)',
                r'Connection refused',
                r'Could not resolve host',
                r'Operation timed out',
                r'Failed to connect',
                r'SSL (certificate|handshake) (error|problem)',
                r'connection reset by peer',
                r'500 Internal Server Error',
                r'404 Not Found',
                r'403 Forbidden'
            ]
            
            for pattern in error_patterns:
                if re.search(pattern, full_response, re.IGNORECASE):
                    match = re.search(pattern, full_response, re.IGNORECASE).group(0)
                    error_message = f"Connection error: {match}"
                    log_with_details('ERROR', error_message)
                    is_success = False
                    
                    # For connection errors, retry if we have attempts left
                    if attempt < retries - 1:
                        retry_timeout = retry_delay * (2 ** attempt)
                        log_with_details('INFO', f"Retrying in {retry_timeout} seconds")
                        time.sleep(retry_timeout)
                        continue
                    return False, stdout_str, error_message
            
            # 3. Process return code - varies by mode
            if not is_success:  # Only check if HTTP status didn't already determine result
                if process.returncode == 0:
                    # Zero return code generally indicates success
                    log_with_details('INFO', "Success based on return code 0")
                    is_success = True
                else:
                    # Non-zero return code - may or may not be an error
                    # The decision is based on the command mode and output
                    
                    # For utilities that modify the input file in-place, allow non-zero codes
                    if mode == 'utility' and ('--output "{input}"' in curl_command or 'output="{input}"' in curl_command):
                        log_with_details('INFO', 
                            f"Utility modifies input file in-place, treating return code {process.returncode} as success")
                        is_success = True
                    # For any mode, substantial stdout suggests it worked despite the error code
                    elif len(stdout_str.strip()) > 100:
                        log_with_details('INFO', 
                            f"Command produced substantial output despite return code {process.returncode}, treating as success")
                        is_success = True
                    # For generators, check if a file was actually produced
                    elif mode == 'generator':
                        video_file = get_latest_video(max_retries=1)
                        if video_file:
                            log_with_details('INFO', 
                                f"Generator produced output file despite return code {process.returncode}, treating as success")
                            is_success = True
                    # For stderr with clear error keywords, treat as failure
                    elif any(keyword in stderr_str.lower() for keyword in ['error', 'failed', 'failure', 'exception']):
                        error_message = f"Process failed with return code {process.returncode}"
                        log_with_details('ERROR', error_message)
                        is_success = False
                    else:
                        # Default behavior depends on mode - uploaders are most sensitive to failures
                        if mode == 'uploader':
                            error_message = f"Upload failed with return code {process.returncode}"
                            log_with_details('ERROR', error_message)
                            is_success = False
                        else:
                            # Generators and utilities often work despite non-zero codes
                            log_with_details('WARNING', 
                                f"Non-zero return code {process.returncode} but no error pattern found, treating as success")
                            is_success = True
            
            # If we're still not successful and have retries left, try again
            if not is_success and attempt < retries - 1:
                retry_timeout = retry_delay * (2 ** attempt)
                log_with_details('INFO', f"Retrying in {retry_timeout} seconds")
                time.sleep(retry_timeout)
                continue
            
            # Either we're successful or out of retries
            if is_success:
                log_with_details('INFO', "Curl command executed successfully")
                
                # For generators, validate the output if requested
                if validate_output and mode == 'generator':
                    # List files in current directory for debugging
                    try:
                        files = os.listdir('.')
                        mp4_files = [f for f in files if f.lower().endswith('.mp4')]
                        log_with_details('INFO', "Files in directory after generator execution", 
                            details={'mp4_files': mp4_files})
                    except Exception as e:
                        log_with_details('WARNING', f"Error listing directory contents: {str(e)}")
                        
                    # Try to find the video file
                    video_file = get_latest_video()
                    if not video_file:
                        if attempt < retries - 1:
                            log_with_details('WARNING', "No video file found, retrying...")
                            time.sleep(retry_delay * (2 ** attempt))
                            continue
                        return False, stdout_str, "No video file generated"
                    
                    log_with_details('INFO', f"Found video file: {video_file}")
                    
                    is_valid, validation_msg = validate_video_file(video_file)
                    if not is_valid:
                        if attempt < retries - 1:
                            log_with_details('WARNING', f"Invalid video file: {validation_msg}, retrying...")
                            time.sleep(retry_delay * (2 ** attempt))
                            continue
                        return False, stdout_str, f"Invalid video file: {validation_msg}"
                
                # Return success result
                return True, stdout_str, generated_filename if mode == 'generator' else stderr_str
            else:
                # We've exhausted retries and still not successful
                return False, stdout_str, error_message or "Failed with unspecified error"
            
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
                                    
            

            
            # Log command details before execution
            log_with_details('INFO', f"Executing command (attempt {attempt+1}/{retries})",
                details={'original_command': curl_command, 'modified_command': modified_command})
            
            try:
                log_with_details('INFO', "Starting Popen subprocess")
                
                # Fix environment path issues on Windows by making sure all paths use forward slashes
                fixed_command = modified_command
                
                # CRITICAL FIX: Check if this is a utility command for IP microservices
                if mode == 'utility' and 'http://' in fixed_command:
                    # Extract the IP and port from the command
                    service_match = re.search(r'http://([0-9.]+):([0-9]+)', fixed_command)
                    if service_match:
                        service_ip = service_match.group(1)
                        service_port = service_match.group(2)
                        log_with_details('INFO', f"Detected microservice call to {service_ip}:{service_port}",
                            details={
                                'service_ip': service_ip,
                                'service_port': service_port,
                                'command': fixed_command
                            })
                        
                        # Verify the service is available before attempting the call
                        try:
                            import socket
                            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                            s.settimeout(5)  # 5 second timeout
                            connect_result = s.connect_ex((service_ip, int(service_port)))
                            s.close()
                            
                            if connect_result == 0:
                                log_with_details('INFO', f"Successfully connected to service at {service_ip}:{service_port}")
                            else:
                                log_with_details('WARNING', f"Cannot connect to service at {service_ip}:{service_port} (error: {connect_result})",
                                    details={'socket_error_code': connect_result})
                                    
                                # Try to add a timeout parameter to curl if not already present
                                if '--connect-timeout' not in fixed_command:
                                    fixed_command = fixed_command.replace('curl', 'curl --connect-timeout 10', 1)
                                    log_with_details('INFO', f"Added connection timeout to curl command",
                                        details={'modified_command': fixed_command})
                        except Exception as socket_err:
                            log_with_details('WARNING', f"Socket test failed: {str(socket_err)}",
                                details={'error': str(socket_err)})
                
                # Setup environment with correct paths
                env = os.environ.copy()
                # If we're in Docker, make sure PATH includes needed directories
                if os.path.exists('/usr/local/bin/curl'):
                    env['PATH'] = f"/usr/local/bin:/usr/bin:/bin:{env.get('PATH', '')}"
                # On Windows, make sure curl.exe is in the PATH
                elif os.name == 'nt' and 'curl' not in env.get('PATH', '').lower():
                    # Try to find curl in system directories
                    curl_paths = [
                        'C:\\Windows\\System32',
                        'C:\\Windows',
                        'C:\\Program Files\\Git\\mingw64\\bin',
                        'C:\\Program Files\\Git\\usr\\bin'
                    ]
                    for p in curl_paths:
                        if os.path.exists(os.path.join(p, 'curl.exe')):
                            env['PATH'] = f"{p};{env.get('PATH', '')}"
                            log_with_details('INFO', f"Added curl.exe path to environment: {p}")
                            break
                
                # Ensure working directory exists (Docker compatibility)
                working_dir = os.getcwd()
                os.makedirs(working_dir, exist_ok=True)
                
                log_with_details('INFO', f"Starting process with env",
                    details={
                        'PATH': env.get('PATH', ''), 
                        'working_dir': working_dir,
                        'command': fixed_command
                    })
                
                # Docker containers should always have curl available
                
                # Run the command
                process = subprocess.Popen(
                    fixed_command,
                    shell=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                    cwd=working_dir  # Set working directory explicitly
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
                
                # Save logs to a backup log file for debugging in case of Docker restart
                if mode == 'generator' or mode == 'utility':
                    try:
                        logs_dir = os.path.join(os.getcwd(), 'backup_logs')
                        os.makedirs(logs_dir, exist_ok=True)
                        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                        log_file = os.path.join(logs_dir, f"{mode}_{timestamp}.log")
                        with open(log_file, 'w', encoding='utf-8') as f:
                            f.write(f"Command: {fixed_command}\n")
                            f.write(f"Return code: {process.returncode}\n")
                            f.write(f"\nSTDOUT:\n{stdout_str}\n")
                            f.write(f"\nSTDERR:\n{stderr_str}\n")
                        log_with_details('INFO', f"Saved {mode} logs to {log_file}",
                            details={'log_file': log_file})
                    except Exception as log_error:
                        log_with_details('WARNING', f"Failed to save backup logs: {str(log_error)}",
                            details={'error': str(log_error)})
                
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
        
        # Check additional directories that might be used by the Docker container
        additional_dirs = ['/tmp', '/var/tmp']
        for add_dir in additional_dirs:
            if os.path.exists(add_dir):
                try:
                    for file in os.listdir(add_dir):
                        if file.lower().endswith('.mp4'):
                            abs_path = os.path.join(add_dir, file)
                            if abs_path not in mp4_files:
                                mp4_files.append(abs_path)
                except Exception as dir_error:
                    cleanup_details[f'dir_listing_error_{add_dir}'] = str(dir_error)
                    log_with_details('WARNING', f"Error listing directory {add_dir}: {str(dir_error)}",
                        details={'dir': add_dir, 'error': str(dir_error)})
        
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
            if os.path.isabs(file):
                abs_path = file
            else:
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
                            # Make sure the file is not in use
                            try:
                                with open(abs_path, 'rb') as test_open:
                                    pass  # Just testing if we can open it
                            except Exception as file_open_error:
                                log_with_details('WARNING', f"File is in use, cannot open: {file}",
                                    details={'file': abs_path, 'error': str(file_open_error)})
                                time.sleep(1)  # Wait a bit before trying again
                                continue
                                
                            # Try to remove the file
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
    """Get most recently created valid MP4 file with Docker compatibility"""
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
        search_paths = []
        
        # Look for all MP4 files in the root directory (simpler approach)
        try:
            current_dir = os.getcwd()
            search_paths.append(current_dir)
            for file in os.listdir(current_dir):
                if file.lower().endswith('.mp4'):
                    abs_path = os.path.abspath(os.path.join(current_dir, file))
                    video_files.append(abs_path)
                    if abs_path not in all_seen_files:
                        all_seen_files.append(abs_path)
        except Exception as e:
            log_with_details('WARNING', f"Error listing current directory: {str(e)}",
                            details={'error': str(e), 'current_dir': current_dir})
        
        # Check additional Docker-compatible paths
        docker_paths = ['/tmp', '/var/tmp']  # Common temp directories in Docker containers
        for docker_path in docker_paths:
            if os.path.exists(docker_path):
                search_paths.append(docker_path)
                try:
                    for file in os.listdir(docker_path):
                        if file.lower().endswith('.mp4'):
                            abs_path = os.path.join(docker_path, file)
                            video_files.append(abs_path)
                            if abs_path not in all_seen_files:
                                all_seen_files.append(abs_path)
                except Exception as e:
                    log_with_details('WARNING', f"Error listing directory {docker_path}: {str(e)}",
                                    details={'error': str(e), 'dir': docker_path})
        
        # Also search in processed_videos directory
        try:
            proc_dir = os.path.join(current_dir, 'processed_videos')
            if os.path.exists(proc_dir) and os.path.isdir(proc_dir):
                search_paths.append(proc_dir)
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
            details={
                'found_files': len(video_files), 
                'files': video_files,
                'search_paths': search_paths
            })
        
        # Filter files by size and make sure they're readable
        valid_videos = []
        for video in video_files:
            try:
                if os.path.exists(video) and os.path.isfile(video):
                    size = os.path.getsize(video)
                    if size >= min_size_bytes:
                        try:
                            # Quick check that the file is readable
                            with open(video, 'rb') as f:
                                header = f.read(8)  # Read first 8 bytes
                                # Check for valid MP4 file signature (ftyp, mdat, free, etc.)
                                has_valid_signature = any(sig in header for sig in [b'ftyp', b'mdat', b'moov', b'free', b'wide', b'skip'])
                                if not has_valid_signature:
                                    log_with_details('WARNING', f"File has invalid MP4 header: {video}",
                                                    details={'header': str(header)})
                                    continue
                            valid_videos.append(video)
                            log_with_details('INFO', f"Validated video file: {video}",
                                            details={'file_size': size, 'path': video})
                        except Exception as file_error:
                            log_with_details('WARNING', f"File cannot be read: {video}",
                                        details={'error': str(file_error)})
                    else:
                        log_with_details('WARNING', f"File too small: {video}",
                                        details={'file_size': size, 'min_size': min_size_bytes})
                else:
                    log_with_details('WARNING', f"File doesn't exist or is not a file: {video}")
            except Exception as e:
                log_with_details('WARNING', f"Error validating file {video}: {str(e)}",
                                details={'error': str(e)})
                
        if valid_videos:
            # Sort by creation time first, and if that's not reliable, by file size
            try:
                # Try to get most recent file by creation time first
                video_timestamps = [(video, os.path.getctime(video)) for video in valid_videos]
                video_timestamps.sort(key=lambda x: x[1], reverse=True)  # Sort by creation time, newest first
                
                if video_timestamps:
                    latest_video = video_timestamps[0][0]
                    creation_time = datetime.fromtimestamp(video_timestamps[0][1])
                    log_with_details('INFO', f"Found valid video file (most recent): {latest_video}",
                                    details={
                                        'creation_time': creation_time.isoformat(),
                                        'file_size': os.path.getsize(latest_video),
                                        'sorted_files': [(v, datetime.fromtimestamp(t).isoformat()) for v, t in video_timestamps[:3]]
                                    })
                    return latest_video
            except Exception as time_error:
                log_with_details('WARNING', f"Error sorting by creation time: {str(time_error)}, trying by size",
                                details={'error': str(time_error)})
                
                # Fall back to sorting by size if timestamp fails
                try:
                    video_sizes = [(video, os.path.getsize(video)) for video in valid_videos]
                    video_sizes.sort(key=lambda x: x[1], reverse=True)  # Sort by size, largest first
                    
                    if video_sizes:
                        largest_video = video_sizes[0][0]
                        size = video_sizes[0][1]
                        log_with_details('INFO', f"Found valid video file (largest): {largest_video}",
                                        details={
                                            'file_size': size,
                                            'sorted_by_size': True,
                                            'sorted_files': [(v, s) for v, s in video_sizes[:3]]
                                        })
                        return largest_video
                except Exception as size_error:
                    log_with_details('ERROR', f"Error sorting by size: {str(size_error)}",
                                    details={'error': str(size_error), 'valid_videos': valid_videos})
            
        # No valid videos found, sleep and try again
        if attempt < max_retries - 1:
            sleep_time = delay * (attempt + 1)  # Increasing backoff
            log_with_details('INFO', f"No valid videos found, retrying in {sleep_time} seconds (attempt {attempt + 1}/{max_retries})")
            time.sleep(sleep_time)
    
    # Final attempt failed
    log_with_details('WARNING', f"Failed to find any valid video files after {max_retries} attempts",
                    details={'all_seen_files': all_seen_files, 'search_paths': search_paths if 'search_paths' in locals() else []})
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
        try:
            # Docker paths should be used directly
            safe_path_unquoted = safe_video_path
            
            formatted_cmd = cmd_template.format(
                video=safe_path_unquoted,
                description=video_title,
                account=platform_data['account_name'],
                sound=task_data['sound_name'],
                volume=task_data['sound_volume'],
                hashtags=hashtags,
                input=safe_path_unquoted  # Also handle the input parameter for utilities
            )
        except KeyError as e:
            # Log missing template parameters
            log_with_details('ERROR', f"Missing template parameter: {str(e)}", 
                details={'cmd_template': cmd_template, 'available_params': {
                    'video': safe_video_path,
                    'description': video_title,
                    'account': platform_data['account_name'],
                    'sound': task_data['sound_name'],
                    'volume': task_data['sound_volume'],
                    'hashtags': hashtags,
                    'input': safe_video_path
                }})
            raise
        
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