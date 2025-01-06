FROM python:3.9-slim

RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

ENV PYTHONPATH=/app
ENV FLASK_APP=webapp.main:app
ENV FLASK_ENV=development
ENV PYTHONUNBUFFERED=1

EXPOSE 5000

CMD ["python", "app/webapp/main.py"]