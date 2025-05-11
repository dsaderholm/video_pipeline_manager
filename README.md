# Video Pipeline Manager

A lightweight video pipeline management system for automating video creation, processing, and uploading through existing APIs.

## Features

- Video Pipeline Management
  - Manage CURL commands for generators, utilities, and uploaders
  - Modular pipeline configuration
  - Video style management

- Multi-Platform Upload Support
  - TikTok, Instagram, YouTube support
  - Primary and dual fallback API support for platforms
  - Cascading fallback system for reliable uploads
  - Platform-specific configurations

- Flexible Scheduling
  - Multiple daily schedules
  - Multi-platform posting

- Dark-themed Web Interface
  - Schedule management
  - CURL command configuration
  - Pipeline monitoring

## Quick Start

1. Configure environment variables in `.env`
2. Build and start the container:
   ```bash
   docker-compose up --build
   ```
3. Access the web interface at http://localhost:5000

## Usage

1. Create Video Styles
   - Define generator commands
   - Set default utilities and configurations

2. Configure Platform Accounts
   - Add primary upload endpoints
   - Configure primary and secondary fallback APIs
   - Set platform-specific settings
   - Configure hashtags and defaults

3. Create Tasks
   - Select video style
   - Choose target platforms
   - Set schedule
   - Configure notifications

## Environment Variables

Copy `.env.example` to `.env` and configure:
- SMTP settings for notifications
- Application configuration
- Upload folder path

## Fallback Systems

### Upload Fallback System

The platform supports a cascading fallback system for uploads:
1. Primary Upload: Main API endpoint for the platform
2. Primary Fallback: First alternative if primary fails
3. Secondary Fallback: Second alternative if both primary and first fallback fail

Each platform can be configured with:
- Required: Primary upload CURL command
- Optional: Primary fallback CURL command
- Optional: Secondary fallback CURL command

The system automatically tries each configured method in sequence until successful upload or all methods are exhausted.

### Night Processing Fallback

The application includes an intelligent fallback system for handling situations when the server is redeployed or restarted during the normal night processing window:

1. **Automatic Detection**: On startup, the system automatically checks if any scheduled tasks from the previous day were missed due to downtime.

2. **Recovery Process**: If missed tasks are detected, the system will automatically generate the videos and immediately upload them, avoiding the need for manual intervention.

3. **Manual Recovery**: If needed, you can trigger a manual recovery using the API endpoint: `POST /api/tasks/recover-missed`

4. **Improved Docker Compatibility**: The system now works reliably in Docker environments, handling file paths and permissions correctly, and searching for generated videos in multiple locations to ensure nothing is missed.

5. **Backup Logs**: All generator and utility operations are logged to backup files in the `backup_logs` directory for better debugging, which survives container restarts.

This ensures that even if the server is redeployed during the day, your scheduled content will still be generated and uploaded appropriately.

## API Documentation

### Platform Endpoints

#### GET /api/platforms
Returns all configured platforms with their upload commands and fallback configurations.

#### POST /api/platforms
Creates a new platform configuration. Parameters:
- platform (required): Platform name
- account_name (required): Account identifier
- uploader_curl (required): Primary upload command
- fallback_curl (optional): Primary fallback command
- fallback_curl_2 (optional): Secondary fallback command
- default_hashtags (optional): Default hashtags for the platform

## Development

### Prerequisites
- Python 3.8+
- Docker and Docker Compose (for containerized deployment)
- SMTP server for notifications

### Local Development
1. Create a virtual environment:
   ```bash
   python -m venv venv
   source venv/bin/activate  # or `venv\Scripts\activate` on Windows
   ```
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Set up environment variables:
   ```bash
   cp .env.example .env
   # Edit .env with your configurations
   ```
4. Initialize the database:
   ```bash
   python reset_db.py
   ```
5. Run the development server:
   ```bash
   python main.py
   ```

### Docker Deployment
1. Build and run with Docker Compose:
   ```bash
   docker-compose up --build
   ```
2. For production, use:
   ```bash
   docker-compose -f docker-compose.yml -f docker-compose.prod.yml up -d
   ```

## Contributing

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add some amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## License

This project is licensed under the MIT License - see the LICENSE file for details.

# Working Version!