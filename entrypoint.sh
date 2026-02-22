#!/bin/bash
set -e

DB_FILE="${DATABASE_PATH:-/app/data/inventory.db}"
DB_DIR=$(dirname "$DB_FILE")
mkdir -p "$DB_DIR"

# Restore from seed if no database exists and seed file is present (>1KB = real file, not LFS pointer)
SEED_FILE="/app/data/seed.sql.gz"
if [ ! -f "$DB_FILE" ] && [ -f "$SEED_FILE" ]; then
    SEED_SIZE=$(stat -f%z "$SEED_FILE" 2>/dev/null || stat -c%s "$SEED_FILE" 2>/dev/null || echo 0)
    if [ "$SEED_SIZE" -gt 1000 ]; then
        echo "No database found. Restoring from seed data..."
        gunzip -c "$SEED_FILE" | sqlite3 "$DB_FILE"
        echo "Seed data restored."
    else
        echo "Seed file appears to be a Git LFS pointer (${SEED_SIZE} bytes). Skipping seed restore."
        echo "Run 'python scheduler.py --full' to populate from live APIs."
    fi
fi

# Initialize DB schema (creates tables if missing, idempotent)
python -c "from db import init_db; init_db()"

echo "Starting supervisord (dashboard + scheduler)..."
exec supervisord -c /app/supervisord.conf
