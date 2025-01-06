FROM python:3.9-slim

RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Debug: Show initial state
RUN echo "Initial directory state:" && ls -la

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code and show what was copied
COPY . .
RUN echo "After COPY . .:" && ls -la && \
    echo "\nContents of current directory:" && ls -R

ENV PYTHONPATH=/app
ENV FLASK_APP=webapp.main:app
ENV FLASK_ENV=development
ENV PYTHONUNBUFFERED=1

EXPOSE 5000

# Final debug command
CMD ["sh", "-c", "echo 'Final container state:' && ls -la && echo '\nRecursive listing:' && ls -R && python webapp/main.py"]