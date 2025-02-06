import os
import sqlite3
import json
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import smtplib
from datetime import datetime
from app import logger

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
        with sqlite3.connect('pipeline.db') as conn:
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
                if "Task started" in message:
                    execution_details['start_time'] = timestamp
                elif "Task completed" in message:
                    execution_details['end_time'] = timestamp
                
                # Track generation time
                if "Video generation completed" in message:
                    if details and 'duration' in details:
                        execution_details['generation_time'] = details['duration']
                
                # Track upload attempts and fallbacks
                if "Attempting upload" in message:
                    platform = details.get('platform', 'unknown')
                    execution_details['upload_attempts'][platform] = execution_details['upload_attempts'].get(platform, 0) + 1
                elif "Using fallback uploader" in message:
                    execution_details['fallback_usage'].append({
                        'platform': details.get('platform'),
                        'reason': details.get('reason'),
                        'timestamp': timestamp
                    })
                
                # Track warnings
                if level == 'WARNING':
                    execution_details['warnings'].append({
                        'message': message,
                        'timestamp': timestamp,
                        'details': details
                    })
                
                # Track upload URLs
                if "Upload successful" in message and details and 'url' in details:
                    platform = details.get('platform', 'unknown')
                    execution_details['upload_urls'][platform] = details['url']
                
                # Track file details
                if "File details" in message and details:
                    execution_details['file_details'].update(details)
                
                # Track processing steps
                if details and 'step' in details:
                    execution_details['processing_steps'].append({
                        'step': details['step'],
                        'status': details.get('status', 'completed'),
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
            
            # Get platform names (keeping existing code)
            platforms = []
            platform_ids = safely_parse_json(task[5], [])
            if platform_ids:
                placeholders = ','.join('?' * len(platform_ids))
                c.execute(f"""
                    SELECT platform, account_name 
                    FROM platform_accounts 
                    WHERE id IN ({placeholders})
                """, platform_ids)
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

def format_task_info_html(task_info, success, base_url):
    """Format task information as HTML matching web UI style"""
    if not task_info:
        return """
        <html>
        <body style='font-family: sans-serif; color: #333;'>
            <h2>Task Status Update</h2>
            <p>The task has {status}, but detailed information is not available.</p>
        </body>
        </html>
        """.format(status="completed successfully" if success else "failed")
        
    status_color = "#22c55e" if success else "#ef4444"  # green-500 or red-500
    utilities_html = "".join([f"<li class='mb-1'><i class='fas fa-wrench mr-2'></i>{u}</li>" for u in task_info['utilities']]) if task_info['utilities'] else "None"
    platforms_html = "".join([f"<li class='mb-1'><i class='fas fa-share-alt mr-2'></i>{p}</li>" for p in task_info['platforms']]) if task_info['platforms'] else "None"
    
    # Format execution details
    execution = task_info.get('execution', {})
    
    # Format warnings section
    warnings_html = ""
    if execution.get('warnings'):
        warnings_html = """
        <div class="section warning-section">
            <h2 class="section-title">Warnings</h2>
            <ul class="warning-list">
        """
        for warning in execution['warnings']:
            warning_details = warning.get('details', {})
            formatted_details = "<br>".join([f"{k}: {v}" for k, v in warning_details.items()]) if warning_details else ""
            warnings_html += f"""
                <li class="warning-item">
                    <span class="warning-time">{warning['timestamp']}</span>
                    <span class="warning-message">{warning['message']}</span>
                    {f'<div class="warning-details">{formatted_details}</div>' if formatted_details else ''}
                </li>
            """
        warnings_html += "</ul></div>"

    # Format upload details
    upload_html = """
    <div class="section">
        <h2 class="section-title">Upload Details</h2>
        <div class="info-grid">
    """
    
    for platform, attempts in execution.get('upload_attempts', {}).items():
        fallback_info = next((f for f in execution.get('fallback_usage', []) if f['platform'] == platform), None)
        url = execution.get('upload_urls', {}).get(platform, 'N/A')
        
        upload_html += f"""
            <span class="label">{platform}:</span>
            <span class="value">
                <div>Attempts: {attempts}</div>
                {f'<div class="fallback-info">Used fallback: {fallback_info["reason"]}</div>' if fallback_info else ''}
                <div>URL: <a href="{url}" target="_blank">{url}</a></div>
            </span>
        """
    upload_html += "</div></div>"

    # Format processing steps
    steps_html = """
    <div class="section">
        <h2 class="section-title">Processing Steps</h2>
        <div class="timeline">
    """
    
    for step in execution.get('processing_steps', []):
        step_color = "#22c55e" if step['status'] == 'completed' else "#ef4444"
        steps_html += f"""
            <div class="timeline-item" style="border-left-color: {step_color}">
                <div class="timeline-header">
                    <span class="step-name">{step['step']}</span>
                    <span class="step-time">{step['timestamp']}</span>
                </div>
                <div class="step-status">{step['status']}</div>
            </div>
        """
    steps_html += "</div></div>"

    # Add file details if available
    file_details_html = ""
    if execution.get('file_details'):
        file_details_html = """
        <div class="section">
            <h2 class="section-title">File Details</h2>
            <div class="info-grid">
        """
        for key, value in execution['file_details'].items():
            file_details_html += f"""
                <span class="label">{key}:</span>
                <span class="value">{value}</span>
            """
        file_details_html += "</div></div>"

    # Add execution timing
    timing_html = """
    <div class="section">
        <h2 class="section-title">Execution Timeline</h2>
        <div class="info-grid">
    """
    if execution.get('start_time'):
        timing_html += f"""
            <span class="label">Started:</span>
            <span class="value">{execution['start_time']}</span>
        """
    if execution.get('end_time'):
        timing_html += f"""
            <span class="label">Completed:</span>
            <span class="value">{execution['end_time']}</span>
        """
    if execution.get('generation_time'):
        timing_html += f"""
            <span class="label">Generation Time:</span>
            <span class="value">{execution['generation_time']} seconds</span>
        """
    timing_html += "</div></div>"

    # Update the CSS to include new styles
    additional_styles = """
        .warning-section { background-color: #2d3748; border-radius: 0.375rem; padding: 1rem; margin-top: 1rem; }
        .warning-list { list-style: none; padding: 0; }
        .warning-item { margin-bottom: 1rem; padding: 0.5rem; border-left: 3px solid #f59e0b; }
        .warning-time { color: #9ca3af; font-size: 0.875rem; display: block; }
        .warning-message { color: #f3f4f6; display: block; margin: 0.25rem 0; }
        .warning-details { color: #9ca3af; font-size: 0.875rem; margin-top: 0.25rem; }
        .timeline { border-left: 2px solid #374151; padding-left: 1.5rem; margin: 1rem 0; }
        .timeline-item { position: relative; margin-bottom: 1.5rem; }
        .timeline-item::before { content: ''; position: absolute; left: -1.75rem; top: 0; width: 1rem; height: 1rem; border-radius: 50%; background: #1f2937; border: 2px solid; }
        .timeline-header { display: flex; justify-content: space-between; align-items: center; }
        .step-name { font-weight: 500; color: #f3f4f6; }
        .step-time { color: #9ca3af; font-size: 0.875rem; }
        .step-status { color: #d1d5db; margin-top: 0.25rem; font-size: 0.875rem; }
        .fallback-info { color: #f59e0b; font-size: 0.875rem; margin: 0.25rem 0; }
    """

    # Combine the original template with the new sections
    return f"""
    <html>
    <head>
        <style>
            body {{
                font-family: sans-serif;
                background-color: #111827;
                color: #f3f4f6;
                padding: 2rem;
                margin: 0;
            }}
            .container {{
                max-width: 800px;  /* Increased from 600px to accommodate more content */
                margin: 0 auto;
                background-color: #1f2937;
                border-radius: 0.5rem;
                padding: 2rem;
                border: 1px solid #374151;
            }}
            /* ... (keep existing styles) ... */
            {additional_styles}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1 class="title">Video Pipeline Pro</h1>
                <div class="status-badge">
                    <i class="fas {('fa-check' if success else 'fa-exclamation-triangle')} mr-2"></i>
                    {task_info['status'].upper()}
                </div>
            </div>

            {timing_html}

            <div class="section">
                <h2 class="section-title">Task Details</h2>
                <div class="info-grid">
                    <span class="label">Name:</span>
                    <span class="value">{task_info['name']}</span>
                    <span class="label">ID:</span>
                    <span class="value">{task_info['id']}</span>
                    <span class="label">Created:</span>
                    <span class="value">{task_info['created_at']}</span>
                </div>
            </div>

            <div class="section">
                <h2 class="section-title">Video Generation</h2>
                <div class="info-grid">
                    <span class="label">Generator:</span>
                    <span class="value">{task_info['generator']}</span>
                    <span class="label">Sound:</span>
                    <span class="value">{task_info['sound_name'] or 'Not specified'} (Volume: {task_info['sound_volume']})</span>
                    <span class="label">Utilities:</span>
                    <span class="value">
                        <ul>
                            {utilities_html}
                        </ul>
                    </span>
                </div>
            </div>

            {file_details_html}
            
            {steps_html}
            
            {upload_html}

            <div class="section">
                <h2 class="section-title">Publishing</h2>
                <div class="info-grid">
                    <span class="label">Platforms:</span>
                    <span class="value">
                        <ul>
                            {platforms_html}
                        </ul>
                    </span>
                    <span class="label">Schedule:</span>
                    <span class="value">{task_info['schedule']}</span>
                    <span class="label">Hashtags:</span>
                    <span class="value">{task_info['hashtags'] or 'None'}</span>
                </div>
            </div>

            {warnings_html if execution.get('warnings') else ''}

            <div class="action-buttons">
                <a href="{base_url}/#/tasks/{task_info['id']}" 
                   class="button primary">
                    <i class="fas fa-external-link-alt mr-2"></i>View Task
                </a>
                
                <a href="{base_url}/#/logs/{task_info['id']}" 
                   class="button secondary">
                    <i class="fas fa-list mr-2"></i>View Logs
                </a>
            </div>
            
            <div class="footer">
                This is an automated notification from your Video Pipeline Manager.<br>
                {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
            </div>
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
        status = "Completed Successfully" if success else "Failed"
        subject = f"Video Pipeline Task {status}: {task_name}"
        
        # Handle multiple email recipients
        to_emails = [email.strip() for email in to_email.split(',') if email.strip()] if to_email else []
        if not to_emails:
            logger.warning(f"No valid email recipients for task {task_id}")
            return False
            
        task_info = get_task_details(task_id)
        
        config = load_smtp_config()
        if not config:
            return False
            
        base_url = config['APP_URL'].rstrip('/')
        message_html = format_task_info_html(task_info, success, base_url)
        
        return send_notification(to_emails, subject, message_html)
    except Exception as e:
        logger.error(f"Error sending task completion notification: {str(e)}")
        return False