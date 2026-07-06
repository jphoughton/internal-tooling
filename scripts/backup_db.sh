#!/bin/bash
# Nightly Railway Postgres backup (launchd: com.hydrant.db-backup, 02:00 daily).
# Restored 2026-07-06 — original was deleted and the job silently failed since Feb 23.
set -euo pipefail

BACKUP_DIR="$HOME/db-backups/hydrant"
ENV_FILE="$HOME/internal-tooling/.env"
LOG="$BACKUP_DIR/backup.log"
KEEP=14

mkdir -p "$BACKUP_DIR"
export PATH="/opt/homebrew/opt/libpq/bin:/opt/homebrew/bin:$PATH"

DATABASE_URL=$(grep -E '^DATABASE_URL=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"')
if [ -z "$DATABASE_URL" ]; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') ERROR: DATABASE_URL not found in $ENV_FILE" >> "$LOG"
  exit 1
fi

STAMP=$(date '+%Y%m%d_%H%M%S')
DUMP="$BACKUP_DIR/hydrant_${STAMP}.dump"

echo "$(date '+%Y-%m-%d %H:%M:%S') Starting backup → $DUMP" >> "$LOG"
pg_dump --verbose --format=custom --no-owner --no-privileges \
  --dbname="$DATABASE_URL" --file="$DUMP" >> "$LOG" 2>&1

shasum -a 256 "$DUMP" > "$DUMP.sha256"
SIZE=$(du -h "$DUMP" | cut -f1)
echo "$(date '+%Y-%m-%d %H:%M:%S') Done: $DUMP ($SIZE)" >> "$LOG"

# retention: keep newest $KEEP dumps
ls -t "$BACKUP_DIR"/hydrant_*.dump 2>/dev/null | tail -n +$((KEEP+1)) | while read -r old; do
  rm -f "$old" "$old.sha256"
  echo "$(date '+%Y-%m-%d %H:%M:%S') Pruned: $old" >> "$LOG"
done
