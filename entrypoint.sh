#!/bin/sh

set -eu

: "${POSTGRES_HOST:?POSTGRES_HOST must be set}"
: "${POSTGRES_PORT:?POSTGRES_PORT must be set}"

# Wait for PostgreSQL
echo "Waiting for PostgreSQL..."
while ! nc -z -w 1 "$POSTGRES_HOST" "$POSTGRES_PORT"; do
  sleep 1
  echo "Still waiting for PostgreSQL..."
done
echo "PostgreSQL is up and running!"

# Apply migrations committed during development
echo "Applying migrations..."
python manage.py migrate --noinput

# Load initial data
echo "Loading initial data..."
python manage.py load_initial_data

# Run the command provided as arguments to this script
exec "$@"
