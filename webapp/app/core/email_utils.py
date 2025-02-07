import os
import sqlite3
import json
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import smtplib
from datetime import datetime
from app import logger

def get_db_path():
    return os.path.join('webapp', 'database', 'pipeline.db')

def load_smtp_config():
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
            
            # Get task with generator name (keeping existing query)
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
            
            # Process logs to extract key information
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
                
                # Track processing steps
                if any(step_msg in message for step_msg in [
                    "Executing generator",
                    "Running utility",
                    "Processing upload for platform"
                ]):
                    step_name = None
                    if "Executing generator" in message:
                        step_name = "Video Generation"
                    elif "Running utility" in message:
                        step_name = f"Utility: {details.get('current_utility', {}).get('name', 'Unknown')}"
                    elif "Processing upload for platform" in message:
                        step_name = f"Upload: {details.get('platform', {}).get('name', 'Unknown')}"
                    
                    execution_details['processing_steps'].append({
                        'step': step_name,
                        'status': 'failed' if level == 'ERROR' else 'completed',
                        'timestamp': timestamp,
                        'details': details
                    })
            
            # Get utilities information (keeping existing code)
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
            
            return {
                'id': task[0],
                'name': task[1],
                'generator': task[-1],
                'utilities': utilities,
                'platforms': platforms,
                'schedule': task[4],
                'hashtags': task[6],
                'sound_name': task[7],
                'sound_volume': task[8],
                'status': task[9],
                'created_at': task[11],
                'execution': execution_details  # New field with detailed execution info
            }
    except Exception as e:
        logger.error(f"Error getting task details: {str(e)}")
        return None

def format_task_info_html(task_info, success, base_url=None):
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
            fallback_info = next((f for f in execution.get('fallback_usage', []) if f['platform'] == platform), None)
            url = execution.get('upload_urls', {}).get(platform, 'N/A')
            upload_html += f"""
                <div class="upload-attempt">
                    <h3>{platform}</h3>
                    <div>Attempts: {attempts}</div>
                    {f'<div class="fallback-info">Used fallback: {fallback_info["reason"]}</div>' if fallback_info else ''}
                    <div>Upload URL: {url}</div>
                </div>
            """

    return f"""
    <html>
    <head>
        <style>
            body {{
                font-family: Inter, sans-serif;
                background-color: #111827;
                color: #f3f4f6;
                padding: 2rem;
                margin: 0;
                line-height: 1.5;
            }}
            h1, h2, h3 {{
                color: #fc4828;
                margin-bottom: 1rem;
            }}
            h1 {{ font-size: 1.5rem; }}
            h2 {{ font-size: 1.2rem; margin-top: 1.5rem; }}
            h3 {{ font-size: 1.1rem; color: #f3f4f6; }}
            .section {{
                background: rgba(31, 41, 55, 0.5);
                border: 1px solid #374151;
                border-radius: 0.5rem;
                padding: 1.5rem;
                margin-bottom: 1.5rem;
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
        </style>
    </head>
    <body>
        <h1>Video Pipeline Pro - Task Report</h1>
        
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

        <div class="section">
            <h2>Task Overview</h2>
            <div class="details-line">Name: {task_info['name']}</div>
            <div class="details-line">ID: {task_info['id']}</div>
            <div class="details-line">Created: {task_info['created_at']}</div>
            <div class="details-line">Status: {task_info['status']}</div>
            {timeline_html}
        </div>

        {f'''
        <div class="section">
            <h2>Upload Results</h2>
            {upload_html}
        </div>
        ''' if upload_html else ''}

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

def send_notification(to_emails, subject, message_html):
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
        with smtplib.SMTP(config['SMTP_SERVER'], int(config['SMTP_PORT'])) as server:
            server.starttls()
            server.login(config['SMTP_USERNAME'], config['SMTP_PASSWORD'])
            
            # Send individual emails to prevent recipients from seeing each other's addresses
            for email in to_emails:
                msg['To'] = email
                try:
                    server.send_message(msg)
                    logger.info(f"Successfully sent email notification to {email}")
                except Exception as e:
                    logger.error(f"Failed to send email to {email}: {str(e)}")
            
        return True
        
    except Exception as e:
        logger.error(f"Failed to send email notifications: {str(e)}")
        return False

def send_task_completion_notification(task_id, task_name, to_email, success=True):
    """Send a notification about task completion status with detailed information"""
    try:
        task_info = get_task_details(task_id)
        if not task_info:
            logger.warning(f"No task info found for task {task_id}")
            return False

        # Determine the detailed status
        execution = task_info.get('execution', {})
        used_fallbacks = bool(execution.get('fallback_usage'))
        preview_mode = task_info.get('status') == 'preview'
        retry_count = len(execution.get('upload_attempts', {})) - 1 if execution.get('upload_attempts') else 0
        
        # Build a descriptive status
        if not success:
            status = "Failed"
        elif preview_mode:
            status = "Preview Generated"
        elif used_fallbacks:
            status = "Completed with Fallbacks"
        else:
            status = "Completed Successfully"

        # Add retry info if relevant
        if retry_count > 0:
            status += f" (After {retry_count} retries)"

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
        message_html = format_task_info_html(task_info, success, base_url)
        
        return send_notification(to_emails, subject, message_html)
    except Exception as e:
        logger.error(f"Error sending task completion notification: {str(e)}")
        return False