FROM python:3.9-slim

RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# First, explicitly create the webapp directory
RUN mkdir -p webapp

# Only copy and install requirements.txt from root
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy only the webapp directory
COPY webapp/ webapp/

ENV PYTHONPATH=/app
ENV FLASK_APP=webapp.main:app
ENV FLASK_ENV=development
ENV PYTHONUNBUFFERED=1

EXPOSE 5000

# Debug commands to see what's going on
CMD ["sh", "-c", "pwd && ls -la && ls -la webapp && python webapp/main.py"]