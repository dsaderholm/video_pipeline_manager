FROM python:3.9-slim

RUN apt-get update && apt-get install -y \
    curl \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Create a non-root user and set up the app directory
RUN useradd -m -U appuser
WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create necessary directories
RUN mkdir -p /app/webapp/previews /app/webapp/processed_videos /app/webapp/database /app/webapp/logs

# Set proper ownership and permissions
RUN chown -R appuser:appuser /app && \
    chmod -R 755 /app

# Switch to appuser for security
USER appuser

ENV PYTHONPATH=/app
ENV FLASK_APP=webapp.core_app:app
ENV FLASK_ENV=production
ENV PYTHONUNBUFFERED=1

EXPOSE 5000

# Use standard Flask server
CMD ["python", "webapp/main.py"]