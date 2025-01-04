FROM python:3.9-slim

RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create data directory
RUN mkdir -p /app/data

ENV PYTHONPATH=/app
ENV FLASK_APP=main.py
ENV FLASK_ENV=development
ENV PYTHONUNBUFFERED=1

EXPOSE 5000

CMD ["python", "main.py"]
