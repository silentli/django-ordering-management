#!/bin/sh

set -eu

# Validate Web runtime configuration without exposing secret values.
python - <<'PY'
import os
import sys

required = (
    'DJANGO_SECRET_KEY', 'ALLOWED_HOSTS', 'STAFF_PASSWORD',
    'POSTGRES_DB', 'POSTGRES_USER', 'POSTGRES_PASSWORD',
    'POSTGRES_HOST', 'POSTGRES_PORT',
)
missing = [name for name in required if not os.environ.get(name, '').strip()]
if missing:
    sys.exit('Missing or empty required environment variables: ' + ', '.join(missing))
PY

POSTGRES_WAIT_TIMEOUT="${POSTGRES_WAIT_TIMEOUT:-60}"

case "$POSTGRES_WAIT_TIMEOUT" in
  *[!0-9]*|'') echo "POSTGRES_WAIT_TIMEOUT must be a positive integer" >&2; exit 1 ;;
esac
if ! [ "$POSTGRES_WAIT_TIMEOUT" -gt 0 ]; then
  echo "POSTGRES_WAIT_TIMEOUT must be a positive integer" >&2
  exit 1
fi

# Wait for PostgreSQL
echo "Waiting for PostgreSQL..."
if ! timeout "$POSTGRES_WAIT_TIMEOUT" sh -c '
  until nc -z -w 1 "$1" "$2"; do sleep 1; done
' sh "$POSTGRES_HOST" "$POSTGRES_PORT"; then
  echo "PostgreSQL was not reachable within ${POSTGRES_WAIT_TIMEOUT}s; startup aborted" >&2
  exit 1
fi
echo "PostgreSQL is up and running!"

# Apply migrations committed during development
echo "Applying migrations..."
python manage.py migrate --noinput

# Run the command provided as arguments to this script
exec "$@"
