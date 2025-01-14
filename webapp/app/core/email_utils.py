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
        raise ValueError(f"Missing required SMTP environment variables: {', '.join(missing_vars)}")
    
    return config

def get_task_details(task_id):
    """Get detailed task information for email"""
    with sqlite3.connect('pipeline.db') as conn:
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
            return None
            
        # Get utilities information
        utilities = []
        if task[3]:  # utilities JSON string
            util_ids = json.loads(task[3])
            if util_ids:
                c.execute("""
                    SELECT name 
                    FROM utilities 
                    WHERE id IN (%s)
                """ % ','.join('?' * len(util_ids)), util_ids)
                utilities = [u[0] for u in c.fetchall()]
        
        # Get platform names
        platforms = []
        if task[5]:  # platforms JSON string
            platform_ids = json.loads(task[5])
            if platform_ids:
                c.execute("""
                    SELECT platform, account_name 
                    FROM platform_accounts 
                    WHERE id IN (%s)
                """ % ','.join('?' * len(platform_ids)), platform_ids)
                platforms = [f"{p[0]} ({p[1]})" for p in c.fetchall()]
        
        return {
            'id': task[0],
            'name': task[1],
            'generator': task[-1],  # generator_name from JOIN
            'utilities': utilities,
            'platforms': platforms,
            'schedule': task[4],
            'hashtags': task[6],
            'sound_name': task[7],
            'sound_volume': task[8],
            'status': task[9],
            'created_at': task[11]
        }

def format_task_info_html(task_info, success, base_url):
    """Format task information as HTML matching web UI style"""
    status_color = "#22c55e" if success else "#ef4444"  # green-500 or red-500
    utilities_html = "".join([f"<li class='mb-1'><i class='fas fa-wrench mr-2'></i>{u}</li>" for u in task_info['utilities']]) if task_info['utilities'] else "None"
    platforms_html = "".join([f"<li class='mb-1'><i class='fas fa-share-alt mr-2'></i>{p}</li>" for p in task_info['platforms']]) if task_info['platforms'] else "None"
    
    # Add action buttons
    action_buttons = f"""
    <div style="display: flex; gap: 1rem; justify-content: center; margin-top: 1.5rem;">
        <a href="{base_url}/#/tasks/{task_info['id']}" 
           style="background-color: #3b82f6; color: white; padding: 0.5rem 1rem; border-radius: 0.375rem; text-decoration: none; font-weight: 500;">
            <i class="fas fa-external-link-alt mr-2"></i>View Task
        </a>
    """

    if not success:
        action_buttons += f"""
        <a href="{base_url}/#/logs/{task_info['id']}" 
           style="background-color: #ef4444; color: white; padding: 0.5rem 1rem; border-radius: 0.375rem; text-decoration: none; font-weight: 500;">
            <i class="fas fa-exclamation-circle mr-2"></i>View Logs
        </a>
        """
    
    action_buttons += "</div>"
    
    return f"""
    <html>
    <head>
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
        <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0-beta3/css/all.min.css" rel="stylesheet">
        <style>
            body {{
                font-family: 'Inter', sans-serif;
                background-color: #111827;
                color: #f3f4f6;
                padding: 2rem;
                margin: 0;
            }}
            .container {{
                max-width: 600px;
                margin: 0 auto;
                background-color: #1f2937;
                border-radius: 0.5rem;
                padding: 2rem;
                border: 1px solid #374151;
            }}
            .header {{
                text-align: center;
                margin-bottom: 2rem;
                padding-bottom: 1rem;
                border-bottom: 1px solid #374151;
            }}
            .title {{
                color: #fc4828;
                font-size: 1.5rem;
                font-weight: 600;
                margin: 0;
            }}
            .status-badge {{
                display: inline-block;
                padding: 0.25rem 0.75rem;
                border-radius: 9999px;
                background-color: {status_color};
                color: white;
                font-size: 0.875rem;
                margin-top: 1rem;
            }}
            .section {{
                margin-bottom: 1.5rem;
            }}
            .section-title {{
                color: #fc4828;
                font-size: 1.1rem;
                font-weight: 500;
                margin-bottom: 0.75rem;
            }}
            .info-grid {{
                display: grid;
                grid-template-columns: auto 1fr;
                gap: 0.5rem;
                margin-bottom: 1rem;
            }}
            .label {{
                color: #9ca3af;
                font-weight: 500;
            }}
            .value {{
                color: #f3f4f6;
            }}
            ul {{
                list-style: none;
                padding: 0;
                margin: 0;
            }}
            .footer {{
                text-align: center;
                margin-top: 2rem;
                padding-top: 1rem;
                border-top: 1px solid #374151;
                color: #9ca3af;
                font-size: 0.875rem;
            }}
            .action-button {{
                display: inline-block;
                padding: 0.5rem 1rem;
                border-radius: 0.375rem;
                text-decoration: none;
                font-weight: 500;
                margin: 0 0.5rem;
            }}
            .action-button.primary {{
                background-color: #3b82f6;
                color: white;
            }}
            .action-button.danger {{
                background-color: #ef4444;
                color: white;
            }}
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

            {action_buttons}
            
            <div class="footer">
                This is an automated notification from your Video Pipeline Manager.<br>
                {' Check the application logs for error details.' if not success else ''}
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
    status = "Completed Successfully" if success else "Failed"
    subject = f"Video Pipeline Task {status}: {task_name}"
    
    # Handle multiple email recipients
    to_emails = [email.strip() for email in to_email.split(',') if email.strip()] if to_email else []
    
    task_info = get_task_details(task_id)
    if not task_info:
        message_html = f"""
        <div style='color: #f3f4f6; font-family: Inter, sans-serif;'>
            Task {task_id} ({task_name}) {status.lower()}, but details are not available.
        </div>
        """
        return send_notification(to_emails, subject, message_html)
    
    config = load_smtp_config()
    base_url = config['APP_URL'].rstrip('/')
    message_html = format_task_info_html(task_info, success, base_url)
    return send_notification(to_emails, subject, message_html)