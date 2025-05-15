import os
import sqlite3
import json
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import smtplib
from datetime import datetime, timedelta
import logging
logger = logging.getLogger('app')

def get_db_path():
    return os.path.join('webapp', 'database', 'pipeline.db')

def load_smtp_config():
    """Load SMTP configuration from environment variables with improved validation"""
    required_vars = [
        'SMTP_SERVER',
        'SMTP_PORT',
        'SMTP_USERNAME',
        'SMTP_PASSWORD',
        'SMTP_FROM_EMAIL'
    ]
    
    # APP_URL is optional for emails
    optional_vars = [
        'APP_URL'
    ]
    
    config = {}
    missing_vars = []
    
    # Check required variables
    for var in required_vars:
        value = os.getenv(var)
        if not value or value.strip() == '':
            missing_vars.append(var)
        config[var] = value
    
    # Add optional variables with defaults
    for var in optional_vars:
        value = os.getenv(var)
        if not value or value.strip() == '':
            if var == 'APP_URL':
                value = 'http://localhost:5898'  # Default value
                logger.info(f"Using default value for {var}: {value}")
        config[var] = value
    
    # Log detailed information about the configuration
    if missing_vars:
        missing_list = ', '.join(missing_vars)
        logger.error(f"Email notifications disabled due to missing SMTP configuration: {missing_list}")
        logger.error("Please check your .env or stack.env file and ensure all required SMTP variables are set.")
        return None
    
    # Validate SMTP port
    try:
        config['SMTP_PORT'] = int(config['SMTP_PORT'])
    except (ValueError, TypeError):
        logger.error(f"Invalid SMTP_PORT value: {config['SMTP_PORT']}. Must be a number.")
        return None
    
    # Securely log partial information for debugging
    username = config['SMTP_USERNAME']
    server = config['SMTP_SERVER']
    masked_password = '●' * 8
    logger.info(f"SMTP configuration loaded - Server: {server}, User: {username}, Password: {masked_password}")
    
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
    """Get detailed task information without relying on logs table"""
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
            
            # Get basic execution details from the task itself
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
            
            # Get generated videos information
            c.execute("""
                SELECT id, original_name, processed_path, scheduled_time, status, upload_status, 
                       generated_at, uploaded_at, error_message
                FROM generated_videos
                WHERE task_id = ?
                ORDER BY generated_at DESC
            """, (task_id,))
            
            videos = c.fetchall()
            if videos:
                # Use video timestamps for execution timeline
                latest_video = videos[0]
                execution_details['start_time'] = latest_video[6]  # generated_at
                
                # Check if uploaded_at exists, use generated_at as fallback
                try:
                    execution_details['end_time'] = latest_video[7] if latest_video[7] else latest_video[6]  # uploaded_at or generated_at
                except IndexError:
                    # If uploaded_at column doesn't exist, use generated_at
                    execution_details['end_time'] = latest_video[6]  # Use generated_at as fallback
                
                # Get file details
                if latest_video[2]:  # processed_path
                    execution_details['file_details'] = {
                        'filename': latest_video[1],  # original_name
                        'path': latest_video[2],      # processed_path
                        'size': 'Generated'           # Placeholder
                    }
                
                # Track uploads
                for video in videos:
                    if video[5] == 'completed':  # upload_status
                        execution_details['upload_attempts']['Platforms'] = 1
                    # Find the error_message index (schema-aware)
                    error_message_index = 8  # Default index based on original schema
                    try:
                        # Only try to access if we have enough elements
                        if len(video) > error_message_index and video[error_message_index]:
                            execution_details['warnings'].append({
                                'message': video[error_message_index],
                                'timestamp': video[6],  # generated_at
                                'details': {}
                            })
                    except IndexError:
                        # Handle case where error_message isn't at expected index
                        logger.warning(f"Error accessing error_message for video {video[0]}")

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
        try:
            # Calculate the scheduled publishing date
            today = datetime.now()
            publish_date = today.strftime('%A, %B %d, %Y')
            
            night_processing_banner = f"""
            <div class="section night-processing">
                <h2>Night Processing Completed</h2>
                <div class="details-line">Your scheduled night processing has completed successfully.</div>
                <div class="details-line"><span class="highlight">Videos are ready for publishing on {publish_date}</span> according to your schedule.</div>
                <div class="details-line" style="margin-top: 10px; font-style: italic;">You'll receive additional notifications when videos are uploaded to their platforms.</div>
            </div>
            """
        except Exception as e:
            logger.error(f"Error formatting night processing banner: {str(e)}")
            # Fallback to a simpler banner without the date
            night_processing_banner = """
            <div class="section night-processing">
                <h2>Night Processing Completed</h2>
                <div class="details-line">Your scheduled night processing has completed successfully.</div>
                <div class="details-line"><span class="highlight">Videos are ready for publishing according to your schedule.</span></div>
                <div class="details-line" style="margin-top: 10px; font-style: italic;">You'll receive additional notifications when videos are uploaded to their platforms.</div>
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

def send_notification(to_emails, subject, message_html):
    """Send an HTML email notification to one or multiple recipients
    
    Args:
        to_emails: String or list of email recipients
        subject: Email subject line
        message_html: HTML content of the email
        
    Returns:
        bool: True if notification was sent successfully, False otherwise
    """
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
    
    # For debugging
    masked_emails = []
    for email in to_emails:
        try:
            if '@' in email:
                masked_emails.append(f"{email[:3]}***{email.split('@')[0][-2:]}@{email.split('@')[1]}")
            else:
                masked_emails.append(f"{email[:3]}***")
        except Exception:
            masked_emails.append("[invalid email format]")
    
    logger.info(f"Preparing to send notification '{subject}' to: {', '.join(masked_emails)}")
        
    try:
        config = load_smtp_config()
        if not config:
            logger.error("Failed to send notification: SMTP configuration not available")
            logger.error("Check your .env or stack.env file to ensure SMTP_SERVER, SMTP_PORT, etc. are set")
            return False
            
        # Create the base message
        msg = MIMEMultipart('alternative')
        msg['From'] = config['SMTP_FROM_EMAIL']
        msg['Subject'] = subject
        
        # Create HTML part
        html_part = MIMEText(message_html, 'html')
        msg.attach(html_part)
        
        # Add plain text alternative for better compatibility
        plain_text = f"Subject: {subject}\n\nThis is an automated notification from your Video Pipeline Manager.\nPlease view this email in an HTML-capable email client for full information.\n\nSent: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        text_part = MIMEText(plain_text, 'plain')
        msg.attach(text_part)
        
        # Connect to SMTP server with detailed logging
        smtp_server = config['SMTP_SERVER']
        smtp_port = config['SMTP_PORT']
        smtp_user = config['SMTP_USERNAME']
        
        logger.info(f"Connecting to SMTP server {smtp_server}:{smtp_port}...")
        logger.info(f"Environment has these settings: SERVER={smtp_server}, PORT={smtp_port}, USER={smtp_user}")
        
        server = None
        try:
            server = smtplib.SMTP(smtp_server, smtp_port, timeout=30)  # Add timeout
        except Exception as conn_e:
            logger.error(f"Failed to establish connection to SMTP server: {str(conn_e)}")
            logger.error(f"Check if the server '{smtp_server}' is valid and reachable")
            return False
        
        # Use TLS for security
        try:
            server.starttls()
            logger.info("TLS connection established successfully")
        except Exception as e:
            logger.error(f"Failed to establish TLS connection: {str(e)}")
            if 'gmail' in smtp_server.lower():
                logger.error("For Gmail, make sure 'Less secure app access' is enabled or use App Passwords")
            if server:
                try: 
                    server.quit()
                except:
                    pass
            return False
        
        # Authenticate with credentials
        try:
            server.login(smtp_user, config['SMTP_PASSWORD'])
            logger.info(f"Successfully authenticated as {smtp_user}")
        except smtplib.SMTPAuthenticationError as auth_e:
            logger.error(f"SMTP Authentication failed: {str(auth_e)}")
            if 'gmail' in smtp_server.lower():
                logger.error("For Gmail, you need to use an App Password. Check .env.example for instructions.")
            elif '535' in str(auth_e):  # Common auth error code
                logger.error("Check your username/password or server security settings")
            if server:
                try: 
                    server.quit()
                except:
                    pass
            return False
        except Exception as e:
            logger.error(f"Unknown login error: {str(e)}")
            if server:
                try: 
                    server.quit()
                except:
                    pass
            return False
            
        # Send to all recipients individually
        success_count = 0
        error_count = 0
        
        for email in to_emails:
            msg['To'] = email
            try:
                server.send_message(msg)
                logger.info(f"Successfully sent email notification to {email}")
                success_count += 1
            except Exception as e:
                error_count += 1
                logger.error(f"Failed to send email to {email}: {str(e)}")
        
        # Close connection
        if server:
            try:
                server.quit()
                logger.info("SMTP connection closed properly")
            except Exception as e:
                logger.warning(f"Error during SMTP connection closure: {str(e)}")
        
        # Report overall status
        if success_count > 0:
            logger.info(f"Email notification summary: {success_count} sent successfully, {error_count} failed")
            return True
        else:
            logger.error("All email notifications failed to send")
            return False
            
    except smtplib.SMTPConnectError as connect_e:
        logger.error(f"SMTP connection error: {str(connect_e)}")
        logger.error(f"Check if the SMTP server {config.get('SMTP_SERVER', 'unknown')} is reachable")
        return False
    except smtplib.SMTPServerDisconnected as disc_e:
        logger.error(f"SMTP server disconnected: {str(disc_e)}")
        return False
    except Exception as e:
        logger.error(f"Failed to send email notifications: {str(e)}")
        logger.error(f"Exception type: {type(e).__name__}")
        return False

def send_task_completion_notification(task_id, task_name, to_email, success=True, platforms=None, night_processing=False):
    """Send a notification about task completion status with detailed information"""
    try:
        task_info = get_task_details(task_id)
        if not task_info:
            logger.warning(f"No task info found for task {task_id}")
            return False

        # Determine the detailed status
        execution = task_info.get('execution', {})
        used_fallbacks = len(execution.get('fallback_usage', [])) > 0
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
        
def send_night_processing_notification(task_summaries, to_email):
    """Send a comprehensive notification about night processing completion
    
    Args:
        task_summaries: List of dicts with task details (id, name, status, video_count)
        to_email: Email address(es) to send notification to
    """
    try:
        # Prepare email data
        now = datetime.now()
        today = now
        today_day = today.strftime('%A')
        
        # Only continue if we have email recipients
        to_emails = [email.strip() for email in to_email.split(',') if email.strip()] if to_email else []
        if not to_emails:
            logger.warning("No valid email recipients for night processing notification")
            return False
            
        # Calculate statistics for summary
        successful_tasks = [t for t in task_summaries if t.get('status') == 'success']
        failed_tasks = [t for t in task_summaries if t.get('status') == 'failed']
        total_videos = sum(t.get('video_count', 0) for t in task_summaries)
        total_tasks = len(task_summaries)
        
        # Create subject line
        if failed_tasks:
            subject = f"Night Processing Completed: {len(successful_tasks)}/{total_tasks} Tasks Successful - {total_videos} Videos Ready"
        else:
            subject = f"Night Processing Completed Successfully: {total_videos} Videos Ready for {today_day}"
        
        # Create HTML message
        message_html = f"""
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
                .night-processing {{
                    background: rgba(25, 47, 89, 0.5);
                    border: 1px solid #3b82f6;
                }}
                .success {{
                    background: rgba(6, 78, 59, 0.5);
                    border: 1px solid #10b981;
                }}
                .failure {{
                    background: rgba(127, 29, 29, 0.5);
                    border: 1px solid #ef4444;
                }}
                .task-list {{
                    display: grid;
                    grid-template-columns: repeat(auto-fill, minmax(250px, 1fr));
                    gap: 1rem;
                    margin-top: 1rem;
                }}
                .task-item {{
                    background: rgba(31, 41, 55, 0.3);
                    padding: 0.75rem;
                    border-radius: 0.375rem;
                }}
                .task-item-success {{
                    border-left: 3px solid #10b981;
                }}
                .task-item-failure {{
                    border-left: 3px solid #ef4444;
                }}
                .task-name {{
                    font-weight: bold;
                    margin-bottom: 0.25rem;
                }}
                .task-details {{
                    font-size: 0.9rem;
                    color: #d1d5db;
                }}
                .summary-stats {{
                    display: flex;
                    gap: 2rem;
                    flex-wrap: wrap;
                    margin: 1rem 0;
                }}
                .stat {{
                    background: rgba(31, 41, 55, 0.3);
                    padding: 1rem;
                    border-radius: 0.375rem;
                    min-width: 120px;
                    text-align: center;
                }}
                .stat-label {{
                    font-size: 0.9rem;
                    color: #d1d5db;
                }}
                .stat-value {{
                    font-size: 1.5rem;
                    font-weight: bold;
                    margin: 0.5rem 0;
                }}
                .highlight {{
                    color: #10b981;
                    font-weight: bold;
                }}
                .footer {{
                    font-size: 0.9rem;
                    color: #9ca3af;
                }}
            </style>
        </head>
        <body>
            <h1>Video Pipeline Pro - Night Processing Report</h1>
            
            <div class="section night-processing">
                <h2>Night Processing Summary</h2>
                <div>Night processing for <span class="highlight">{today_day}</span> has completed.</div>
                
                <div class="summary-stats">
                    <div class="stat">
                        <div class="stat-label">Tasks Processed</div>
                        <div class="stat-value">{len(successful_tasks)}/{total_tasks}</div>
                    </div>
                    <div class="stat">
                        <div class="stat-label">Videos Generated</div>
                        <div class="stat-value">{total_videos}</div>
                    </div>
                    <div class="stat">
                        <div class="stat-label">Schedule Date</div>
                        <div class="stat-value">{today_day}</div>
                    </div>
                </div>
            </div>
            
            {f'''<div class="section success">''' if successful_tasks else '''<div class="section">'''}
                <h2>Successful Tasks</h2>
                {f'''<div>All {len(successful_tasks)} tasks processed successfully.</div>''' if len(successful_tasks) == total_tasks else f'''<div>{len(successful_tasks)} out of {total_tasks} tasks processed successfully.</div>'''}
                
                <div class="task-list">
                    {chr(10).join(f'''
                    <div class="task-item task-item-success">
                        <div class="task-name">{task['name']}</div>
                        <div class="task-details">ID: {task['id']} | Videos: {task['video_count']}</div>
                        <div class="task-details">Scheduled for {today_day}</div>
                    </div>''' for task in successful_tasks) if successful_tasks else '<div>No successful tasks.</div>'}
                </div>
            </div>
            
            {f'''<div class="section failure">''' if failed_tasks else '''<div class="section" style="display: none;">'''}
                <h2>Failed Tasks</h2>
                <div>{len(failed_tasks)} tasks encountered errors during processing.</div>
                
                <div class="task-list">
                    {chr(10).join(f'''
                    <div class="task-item task-item-failure">
                        <div class="task-name">{task['name']}</div>
                        <div class="task-details">ID: {task['id']}</div>
                        <div class="task-details">Error: {task.get('error', 'Unknown error')}</div>
                    </div>''' for task in failed_tasks) if failed_tasks else '<div>No failed tasks.</div>'}
                </div>
            </div>
            
            <div class="section">
                <h2>Next Steps</h2>
                <p>The videos generated during night processing are now ready to be uploaded according to their scheduled times.</p>
                <p>You can review these videos in the Video Pipeline Manager dashboard and make any necessary adjustments before they are automatically uploaded.</p>
            </div>

            <div class="footer section">
                <div>This is an automated notification from your Video Pipeline Manager.</div>
                <div>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</div>
            </div>
        </body>
        </html>
        """
        
        # Send the notification
        config = load_smtp_config()
        if not config:
            return False
            
        return send_notification(to_emails, subject, message_html)
    except Exception as e:
        logger.error(f"Error sending night processing notification: {str(e)}")
        return False