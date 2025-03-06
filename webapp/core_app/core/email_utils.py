import os
import sqlite3
import json
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import smtplib
from datetime import datetime
import logging
logger = logging.getLogger('app')

def get_db_path():
    return os.path.join('webapp', 'database', 'pipeline.db')

def load_smtp_config(silent=False):
    """Load SMTP configuration with optional silent mode
    
    Args:
        silent (bool): If True, suppresses logging warnings
    """
    """Load SMTP configuration from environment variables"""
    required_vars = [
        'SMTP_SERVER',
        'SMTP_PORT',
        'SMTP_USERNAME',
        'SMTP_PASSWORD',
        'SMTP_FROM_EMAIL',
        'APP_URL'  # Add this for UI links
    ]
    
    config = {}
    missing_vars = []
    
    for var in required_vars:
        value = os.getenv(var)
        if value is None:
            missing_vars.append(var)
        config[var] = value
    
    if missing_vars:
        if not silent:
            logger.warning(f"Missing SMTP environment variables: {', '.join(missing_vars)}")
        return None
    
    return config

def safely_parse_json(json_str, default=None):
    """Safely parse JSON string with fallback to default"""
    if not json_str:
        return default
    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        logger.warning(f"Failed to parse JSON: {json_str}")
        return default

def get_task_details(task_id):
    """Get detailed task information including execution data for email"""
    try:
        with sqlite3.connect(get_db_path()) as conn:
            c = conn.cursor()
            
            # Get task with generator name
            c.execute("""
                SELECT t.*, g.name as generator_name 
                FROM tasks t
                JOIN generators g ON t.generator_id = g.id
                WHERE t.id = ?
            """, (task_id,))
            task = c.fetchone()
            
            if not task:
                logger.warning(f"No task found with ID {task_id}")
                return None
            
            # Get task logs for execution details
            c.execute("""
                SELECT timestamp, level, message, details, source
                FROM logs
                WHERE task_id = ?
                ORDER BY timestamp ASC
            """, (task_id,))
            logs = c.fetchall()
            
            # Process logs to extract key information (keeping existing execution_details code)
            execution_details = {
                'start_time': None,
                'end_time': None,
                'generation_time': None,
                'upload_attempts': {},
                'fallback_usage': [],
                'warnings': [],
                'upload_urls': {},
                'file_details': {},
                'processing_steps': []
            }
            
            for log in logs:
                timestamp, level, message, details_json, source = log
                details = safely_parse_json(details_json, {})
                
                # Track execution timeline
                if "Starting video pipeline" in message:
                    execution_details['start_time'] = timestamp
                elif "Task completed successfully" in message:
                    execution_details['end_time'] = timestamp
                
                # Track generation time
                if "Generator completed successfully" in message:
                    if details:
                        execution_details['generation_time'] = details.get('duration_seconds')
                
                # Track upload attempts and fallbacks
                if "Processing upload for platform" in message:
                    platform = details.get('platform', {}).get('name', 'unknown')
                    execution_details['upload_attempts'][platform] = execution_details['upload_attempts'].get(platform, 0) + 1
                elif "Primary upload failed, attempting fallback" in message:
                    execution_details['fallback_usage'].append({
                        'platform': details.get('platform', {}).get('name'),
                        'reason': details.get('stderr', 'Primary upload failed'),
                        'fallback_level': 1,
                        'timestamp': timestamp
                    })
                elif "Fallback upload failed, attempting secondary fallback" in message:
                    execution_details['fallback_usage'].append({
                        'platform': details.get('platform', {}).get('name'),
                        'reason': details.get('stderr', 'Fallback upload failed'),
                        'fallback_level': 2,
                        'timestamp': timestamp
                    })
                
                # Track warnings
                if level == 'WARNING':
                    execution_details['warnings'].append({
                        'message': message,
                        'timestamp': timestamp,
                        'details': details
                    })
                
                # Track file details from validation
                if "Video file validation successful" in message:
                    execution_details['file_details'].update(details)

            # Get utilities information
            utilities = []
            util_ids = safely_parse_json(task[3], [])
            if util_ids:
                placeholders = ','.join('?' * len(util_ids))
                c.execute(f"SELECT name FROM utilities WHERE id IN ({placeholders})", util_ids)
                utilities = [u[0] for u in c.fetchall()]
            
            # Get platforms and their account names
            platforms = []
            c.execute("""
                SELECT p.name, tpa.account_name 
                FROM task_platform_accounts tpa
                JOIN platforms p ON tpa.platform_id = p.id
                WHERE tpa.task_id = ?
            """, (task_id,))
            platforms = [f"{p[0]} ({p[1]})" for p in c.fetchall()]
            
            # Note: task columns are:
            # 0: id
            # 1: name
            # 2: generator_id
            # 3: utilities
            # 4: schedule
            # 5: hashtags
            # 6: sound_name
            # 7: sound_volume
            # 8: status
            # 9: email_notify
            # 10: retry_count
            # 11: created_at
            # 12: generator_name (from JOIN)
            
            return {
                'id': task[0],
                'name': task[1],
                'generator': task[-1],  # This is generator_name from the JOIN
                'utilities': utilities,
                'platforms': platforms,
                'schedule': task[4],
                'hashtags': task[5],
                'sound_name': task[6],  # This maps to the correct sound_name field
                'sound_volume': task[7],  # This maps to the correct sound_volume field
                'status': task[8],
                'created_at': task[11],
                'execution': execution_details
            }
    except Exception as e:
        logger.error(f"Error getting task details: {str(e)}")
        return None

def format_task_info_html(task_info, success, base_url=None, night_processing=False):
    """Generate a comprehensive and visually appealing HTML email report
    
    Prioritizes key information and provides clear, contextual insights
    """
    from datetime import datetime
    import json
    
    def calculate_time_since(timestamp):
        """Calculate human-readable time difference"""
        try:
            created_time = datetime.fromisoformat(timestamp) if timestamp else datetime.now()
            now = datetime.now()
            diff = now - created_time
            
            if diff.days > 0:
                return f"{diff.days} day{'s' if diff.days > 1 else ''} ago"
            elif diff.seconds // 3600 > 0:
                hours = diff.seconds // 3600
                return f"{hours} hour{'s' if hours > 1 else ''} ago"
            elif diff.seconds // 60 > 0:
                minutes = diff.seconds // 60
                return f"{minutes} minute{'s' if minutes > 1 else ''} ago"
            else:
                return "just now"
        except Exception:
            return "timestamp unavailable"
    
    def create_status_badge(status, is_success):
        """Create a visually distinct status badge"""
        status_class = 'status-success' if is_success else 'status-error'
        return f'<span class="status-indicator {status_class}">{status}</span>'
    """Generate a comprehensive HTML email report with improved layout and information hierarchy.
    
    Priority Ordering:
    1. High-level task status and overview
    2. Upload results and platform performance
    3. Video generation details
    4. Configuration and settings
    5. Additional insights and warnings
    """
    if not task_info:
        return """
        <html>
        <body style='font-family: Inter, sans-serif; background-color: #111827; color: #f3f4f6;'>
            <h2>Task Status Update</h2>
            <p>The task has {status}, but detailed information is not available.</p>
        </body>
        </html>
        """.format(status="completed successfully" if success else "failed")
    
    # Format utilities list
    utilities_html = ""
    for util in task_info['utilities']:
        utilities_html += f"""
            <li class="utility-item">
                <div class="util-name">{util}</div>
            </li>
        """
    if not utilities_html:
        utilities_html = "<li>None</li>"

    # Format platforms list
    platforms_html = ""
    for platform in task_info['platforms']:
        platforms_html += f"""
            <li class="platform-item">
                <div class="platform-name">{platform}</div>
            </li>
        """
    if not platforms_html:
        platforms_html = "<li>None</li>"

    # Get execution details
    execution = task_info.get('execution', {})
    
    # Format execution timeline
    timeline_html = "<div class='timeline'>"
    if execution.get('start_time'):
        timeline_html += f"<div>Started: {execution['start_time']}</div>"
    if execution.get('end_time'):
        timeline_html += f"<div>Completed: {execution['end_time']}</div>"
    if execution.get('generation_time'):
        timeline_html += f"<div>Generation Time: {execution['generation_time']} seconds</div>"
    timeline_html += "</div>"

    # Format sound information
    sound_html = f"Sound: {task_info.get('sound_name', 'N/A')}"
    volume_html = f"Volume: {task_info.get('sound_volume', 'N/A')}"

    # Format upload attempts
    upload_html = ""
    if execution.get('upload_attempts'):
        for platform, attempts in execution['upload_attempts'].items():
            # Get all fallback infos for this platform to show progression
            platform_fallbacks = [f for f in execution.get('fallback_usage', []) if f['platform'] == platform]
            url = execution.get('upload_urls', {}).get(platform, 'N/A')
            
            fallback_html = ""
            if platform_fallbacks:
                for fallback in platform_fallbacks:
                    fallback_level = fallback.get('fallback_level', 1)
                    fallback_name = "Primary" if fallback_level == 1 else "Secondary"
                    fallback_html += f"<div class=\"fallback-info\">Used {fallback_name} fallback: {fallback['reason']}</div>"
            
            upload_html += f"""
                <div class="upload-attempt">
                    <h3>{platform}</h3>
                    <div>Attempts: {attempts}</div>
                    {fallback_html}
                    <div>Upload URL: {url}</div>
                </div>
            """

    # Create a highlighted banner for night processing if applicable
    night_processing_banner = ""
    if night_processing:
        night_processing_banner = f"""
        <div class="section night-processing">
            <h2>Night Processing Completed</h2>
            <div class="details-line">Your scheduled night processing has completed successfully.</div>
            <div class="details-line">Videos are ready for publishing according to your schedule.</div>
        </div>
        """
    
    return f"""
    <html>
    <head>
        <style>
            body {
                font-family: 'Inter', 'Helvetica Neue', Arial, sans-serif;
                background-color: #0f172a;
                color: #e2e8f0;
                padding: 2rem;
                margin: 0;
                line-height: 1.6;
                max-width: 800px;
                margin: 0 auto;
            }
            .container {
                background-color: #1e293b;
                border-radius: 0.75rem;
                box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.25);
                overflow: hidden;
            }
            h1, h2, h3 {
                color: #38bdf8;
                margin-bottom: 1rem;
                font-weight: 600;
            }
            h1 {
                font-size: 1.8rem;
                border-bottom: 2px solid #334155;
                padding-bottom: 0.5rem;
            }
            h2 {
                font-size: 1.4rem;
                margin-top: 1.5rem;
                color: #22d3ee;
            }
            h3 {
                font-size: 1.2rem;
                color: #67e8f9;
            }
            .section {
                background: rgba(30, 41, 59, 0.7);
                border-bottom: 1px solid #334155;
                padding: 1.5rem;
                margin-bottom: 0;
            }
            .status-indicator {
                display: inline-block;
                padding: 0.25rem 0.75rem;
                border-radius: 9999px;
                font-weight: 600;
                font-size: 0.875rem;
                margin-left: 0.5rem;
            }
            .status-success {
                background-color: rgba(16, 185, 129, 0.2);
                color: #10b981;
            }
            .status-warning {
                background-color: rgba(245, 158, 11, 0.2);
                color: #f59e0b;
            }
            .status-error {
                background-color: rgba(239, 68, 68, 0.2);
                color: #ef4444;
            }
            .night-processing {{
                background: rgba(25, 47, 89, 0.5);
                border: 1px solid #3b82f6;
            }}
            .sound-volume {{
                display: flex;
                gap: 1rem;
                margin-bottom: 0.5rem;
            }}
            ul {{
                list-style: none;
                padding: 0;
                margin: 0;
            }}
            li {{
                margin-bottom: 0.75rem;
            }}
            .utility-item, .platform-item {{
                background: rgba(31, 41, 55, 0.3);
                padding: 0.75rem;
                border-radius: 0.375rem;
                margin-bottom: 0.5rem;
            }}
            .timeline {{
                border-left: 2px solid #374151;
                padding-left: 1rem;
                margin: 1rem 0;
            }}
            .timeline div {{
                margin-bottom: 0.5rem;
            }}
            .upload-attempt {{
                background: rgba(31, 41, 55, 0.3);
                padding: 1rem;
                border-radius: 0.375rem;
                margin-bottom: 1rem;
            }}
            .fallback-info {{
                color: #fbbf24;
                margin: 0.5rem 0;
            }}
            .details-line {{
                margin: 0.5rem 0;
            }}
            .highlight {{
                color: #10b981;
                font-weight: bold;
            }}
        </style>
    </head>
    <body>
        <h1>Video Pipeline Pro - Task Report</h1>
        
        {night_processing_banner}
        
        <div class="section">
            <h2>Task Overview</h2>
            <div class="details-line">Name: <span class="highlight">{task_info['name']}</span></div>
            <div class="details-line">ID: {task_info['id']}</div>
            <div class="details-line">Status: <span class="highlight">{task_info['status']}</span></div>
            <div class="details-line">Created: {task_info['created_at']}</div>
            {timeline_html}
        </div>

        {f'''
        <div class="section">
            <h2>Upload Results</h2>
            {upload_html}
        </div>
        ''' if upload_html else ''}

        <div class="section">
            <h2>Video Generation</h2>
            <div class="details-line">Generator: {task_info.get('generator', 'N/A')}</div>
            <div class="sound-volume">
                <div class="details-line">{sound_html}</div>
                <div class="details-line">{volume_html}</div>
            </div>
            <h3>Utilities:</h3>
            <ul>
                {utilities_html}
            </ul>
        </div>

        <div class="section">
            <h2>Publishing Configuration</h2>
            <div class="details-line">Schedule: {task_info.get('schedule', 'N/A')}</div>
            <div class="details-line">Hashtags: {task_info.get('hashtags', 'N/A')}</div>
            <h3>Target Platforms:</h3>
            <ul>
                {platforms_html}
            </ul>
        </div>

        {f'''
        <div class="section">
            <h2>File Details</h2>
            <pre>{json.dumps(execution.get('file_details', {}), indent=2)}</pre>
        </div>
        ''' if execution.get('file_details') else ''}

        {f'''
        <div class="section">
            <h2>Warnings & Issues</h2>
            <ul>
                {chr(10).join(f'<li>{w["message"]}</li>' for w in execution.get('warnings', []))}
            </ul>
        </div>
        ''' if execution.get('warnings') else ''}

        <div class="footer section">
            <div>This is an automated notification from your Video Pipeline Manager.</div>
            <div>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</div>
        </div>
    </body>
    </html>
    """

def send_notification(to_emails, subject, message_html, retry_count=2):
    """Send an HTML email notification with optional retry mechanism
    
    Args:
        to_emails (list or str): Recipient email(s)
        subject (str): Email subject
        message_html (str): HTML content of the email
        retry_count (int): Number of retry attempts on failure
    """
    """Send an HTML email notification to one or multiple recipients"""
    if not to_emails:
        logger.warning("No recipient emails provided, skipping notification")
        return False
    
    # Convert single email to list
    if isinstance(to_emails, str):
        to_emails = [to_emails]
    
    # Remove any empty strings or None values
    to_emails = [email.strip() for email in to_emails if email and email.strip()]
    
    if not to_emails:
        logger.warning("No valid recipient emails after cleaning, skipping notification")
        return False
        
    try:
        config = load_smtp_config()
        if not config:
            logger.warning("SMTP configuration not available, skipping notification")
            return False
            
        # Create the base message
        msg = MIMEMultipart('alternative')
        msg['From'] = config['SMTP_FROM_EMAIL']
        msg['Subject'] = subject
        
        # Create HTML part
        html_part = MIMEText(message_html, 'html')
        msg.attach(html_part)
        
        # Send to all recipients (BCC)
        for attempt in range(retry_count + 1):
            try:
                with smtplib.SMTP(config['SMTP_SERVER'], int(config['SMTP_PORT'])) as server:
                    server.starttls()
                    server.login(config['SMTP_USERNAME'], config['SMTP_PASSWORD'])
                    
                    # Send individual emails to prevent recipients from seeing each other's addresses
                    sent_emails = []
                    for email in to_emails:
                        msg['To'] = email
                        try:
                            server.send_message(msg)
                            logger.info(f"Successfully sent email notification to {email}")
                            sent_emails.append(email)
                        except Exception as inner_e:
                            logger.error(f"Failed to send email to {email}: {str(inner_e)}")
                    
                    return len(sent_emails) > 0
            except Exception as e:
                if attempt < retry_count:
                    delay = (attempt + 1) * 2  # Exponential backoff
                    logger.warning(f"SMTP connection failed (attempt {attempt + 1}/{retry_count + 1}). Retrying in {delay} seconds: {str(e)}")
                    import time
                    time.sleep(delay)
                else:
                    logger.error(f"Failed to send email after {retry_count + 1} attempts: {str(e)}")
        
        return False
        
    except Exception as e:
        logger.error(f"Failed to send email notifications: {str(e)}")
        return False

def send_task_completion_notification(task_id, task_name, to_email, success=True, platforms=None, night_processing=False):
    """Send a notification about task completion status with detailed information"""
    try:
        task_info = get_task_details(task_id)
        if not task_info:
            logger.warning(f"No task info found for task {task_id}")
            return False

        # Enhanced status determination with more nuanced categorization
        execution = task_info.get('execution', {})
        used_fallbacks = len(execution.get('fallback_usage', [])) > 0
        preview_mode = task_info.get('status') == 'preview'
        retry_count = len(execution.get('upload_attempts', {})) - 1 if execution.get('upload_attempts') else 0

        # Categorize task outcome more precisely
        if not success:
            status = "Failed"
            status_class = 'status-error'
        elif preview_mode:
            status = "Preview Generated"
            status_class = 'status-warning'
        elif used_fallbacks:
            status = "Completed with Fallbacks"
            status_class = 'status-warning'
        else:
            status = "Completed Successfully"
            status_class = 'status-success'

        # Add retry info if relevant
        if retry_count > 0:
            status += f" (After {retry_count} retries)"

        # Add platforms info if provided
        if platforms:
            status += f" - Uploaded to {', '.join(platforms)}"

        subject = f"Video Pipeline Task {status}: {task_name}"
        
        # Handle multiple email recipients
        to_emails = [email.strip() for email in to_email.split(',') if email.strip()] if to_email else []
        if not to_emails:
            logger.warning(f"No valid email recipients for task {task_id}")
            return False
            
        config = load_smtp_config()
        if not config:
            return False
            
        base_url = config['APP_URL'].rstrip('/')
        message_html = format_task_info_html(task_info, success, base_url, night_processing)
        
        return send_notification(to_emails, subject, message_html)
    except Exception as e:
        logger.error(f"Error sending task completion notification: {str(e)}")
        return False