FROM python:3.9-slim

RUN apt-get update && apt-get install -y \
    curl \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Create a non-root user and set up the app directory
RUN useradd -m -U appuser
WORKDIR /app

# Debug: Show initial state
RUN echo "Initial directory state:" && ls -la

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code and show what was copied
COPY . .

# Set proper ownership and permissions
RUN chown -R appuser:appuser /app && \
    chmod -R 755 /app

# Switch to appuser for security
USER appuser

ENV PYTHONPATH=/app
ENV FLASK_APP=webapp.main:app
ENV FLASK_ENV=development
ENV PYTHONUNBUFFERED=1

EXPOSE 5000

# Make sure working directory is writable
RUN touch /app/.test && rm /app/.test

# Final debug command
CMD ["python", "webapp/main.py"]