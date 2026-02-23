#!/bin/bash
set -e

# Wait briefly for Railway to inject environment variables
if [ -z "$DATABASE_URL" ]; then
    echo "ERROR: DATABASE_URL is not set."
    echo "Add a PostgreSQL database to your Railway project and link DATABASE_URL to this service."
    exit 1
fi

# Initialize DB schema (creates tables if missing, idempotent)
echo "Initializing PostgreSQL schema..."
python -c "from db import init_db; init_db()"
echo "Schema ready."

echo "Starting supervisord (dashboard + scheduler)..."
exec supervisord -c /app/supervisord.conf
