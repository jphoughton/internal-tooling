#!/bin/bash
set -e

DB_FILE="${DATABASE_PATH:-/app/data/inventory.db}"
DB_DIR=$(dirname "$DB_FILE")
mkdir -p "$DB_DIR"

# Restore from seed if database is missing or empty, and seed file is present
SEED_FILE="/app/data/seed.sql.gz"
NEEDS_SEED=false

if [ ! -f "$DB_FILE" ]; then
    NEEDS_SEED=true
elif [ -f "$DB_FILE" ]; then
    # Check if DB has any order data — if empty, re-seed
    ROW_COUNT=$(sqlite3 "$DB_FILE" "SELECT COUNT(*) FROM orders;" 2>/dev/null || echo 0)
    if [ "$ROW_COUNT" -eq 0 ]; then
        echo "Database exists but has no data. Will re-seed."
        rm -f "$DB_FILE" "$DB_FILE-shm" "$DB_FILE-wal"
        NEEDS_SEED=true
    fi
fi

if [ "$NEEDS_SEED" = true ] && [ -f "$SEED_FILE" ]; then
    SEED_SIZE=$(stat -f%z "$SEED_FILE" 2>/dev/null || stat -c%s "$SEED_FILE" 2>/dev/null || echo 0)
    if [ "$SEED_SIZE" -gt 1000 ]; then
        echo "Restoring from seed data..."
        gunzip -c "$SEED_FILE" | sqlite3 "$DB_FILE"
        echo "Seed data restored."
    else
        echo "Seed file appears to be a Git LFS pointer (${SEED_SIZE} bytes). Skipping seed restore."
    fi
fi

# Initialize DB schema (creates tables if missing, idempotent)
python -c "from db import init_db; init_db()"

echo "Starting supervisord (dashboard + scheduler)..."
exec supervisord -c /app/supervisord.conf
