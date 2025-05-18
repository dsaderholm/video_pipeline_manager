#!/bin/bash
# Script to help with initial PostgreSQL setup
# This will run inside the web container to check if it can connect to PostgreSQL
# and initialize the database if needed

# Wait for PostgreSQL to be ready
echo "Waiting for PostgreSQL to start..."
MAX_ATTEMPTS=30
ATTEMPT=0

while [ $ATTEMPT -lt $MAX_ATTEMPTS ]; do
    ATTEMPT=$((ATTEMPT + 1))
    echo "Attempt $ATTEMPT of $MAX_ATTEMPTS..."
    pg_isready -h db -U postgres
    if [ $? -eq 0 ]; then
        echo "PostgreSQL is ready!"
        break
    fi
    if [ $ATTEMPT -eq $MAX_ATTEMPTS ]; then
        echo "Failed to connect to PostgreSQL after $MAX_ATTEMPTS attempts. Exiting."
        exit 1
    fi
    sleep 2
done

# Check if our database exists
echo "Checking if database exists..."
psql -h db -U postgres -lqt | cut -d \| -f 1 | grep -qw video_pipeline

if [ $? -ne 0 ]; then
    echo "Creating video_pipeline database..."
    psql -h db -U postgres -c "CREATE DATABASE video_pipeline"
else
    echo "Database already exists."
fi

# Initialize database tables
echo "Running database initialization..."
python -c "from webapp.core_app.models import init_db; init_db()"

echo "Database setup complete!"
