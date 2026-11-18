#!/bin/sh

# Wait for PostgreSQL
echo "Waiting for PostgreSQL..."
while ! nc -z -w 1 $POSTGRES_HOST $POSTGRES_PORT; do
  sleep 1
  echo "Still waiting for PostgreSQL..."
done
echo "PostgreSQL is up and running!"

# Create and apply migrations
echo "Creating migrations..."
python manage.py makemigrations

echo "Applying migrations..."
python manage.py migrate

# Load initial data
echo "Loading initial data..."
python manage.py load_initial_data

# Run the command provided as arguments to this script
exec "$@"
