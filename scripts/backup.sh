#!/usr/bin/env bash
# Consistent backup of the SQLite database + ticket PDFs (safe while the app is running).
# Usage: ./scripts/backup.sh [backup-dir]      Cron example: 0 3 * * * /home/ubuntu/gate/scripts/backup.sh
set -euo pipefail
cd "$(dirname "$0")/.."
DEST="${1:-$HOME/gate-backups}"
STAMP="$(date +%Y%m%d-%H%M%S)"
mkdir -p "$DEST"

# online, WAL-safe copy using SQLite's backup API
docker compose exec -T app python - <<'PY'
import sqlite3
src = sqlite3.connect("/srv/data/app.db")
dst = sqlite3.connect("/srv/data/backup.db")
src.backup(dst)
dst.close(); src.close()
PY
docker compose cp app:/srv/data/backup.db "$DEST/app-$STAMP.db"
docker compose exec -T app rm -f /srv/data/backup.db
docker compose exec -T app tar -C /srv -cz storage > "$DEST/storage-$STAMP.tar.gz"

# keep the newest 14 of each
ls -1t "$DEST"/app-*.db 2>/dev/null | tail -n +15 | xargs -r rm --
ls -1t "$DEST"/storage-*.tar.gz 2>/dev/null | tail -n +15 | xargs -r rm --
echo "Backup written to $DEST ($STAMP)"
