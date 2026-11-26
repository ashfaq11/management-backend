#!/usr/bin/env bash
# Nightly logical backup of the spa schema (cron): keeps 14 compressed dumps in /var/lib/spa/backups.
# Only for the Postgres container on this box; with an external/managed database use its own backups.
set -euo pipefail
docker inspect spa-pg >/dev/null 2>&1 || { echo "no local spa-pg container; skipping"; exit 0; }
mkdir -p /var/lib/spa/backups
f=/var/lib/spa/backups/spa-$(date +%F).sql.gz
docker exec spa-pg pg_dump -U spa -d spa --schema=spa | gzip > "$f.tmp" && mv "$f.tmp" "$f"
ls -1t /var/lib/spa/backups/spa-*.sql.gz | tail -n +15 | xargs -r rm --
# restore: see restore.sh (it also reinstalls the btree_gist extension a schema-only dump omits)
