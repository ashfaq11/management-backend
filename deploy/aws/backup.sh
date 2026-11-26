#!/usr/bin/env bash
# Nightly consistent SQLite backup (cron): keeps 14 copies in /var/lib/spa/backups.
set -euo pipefail
docker exec spa-api python - <<'PY'
import sqlite3, datetime, os
os.makedirs("/data/backups", exist_ok=True)  # as the app user, so it can write there
src = sqlite3.connect("/data/spa.db")
dst = sqlite3.connect(f"/data/backups/spa-{datetime.date.today()}.db")
src.backup(dst)
PY
ls -1t /var/lib/spa/backups/spa-*.db | tail -n +15 | xargs -r rm --
