#!/usr/bin/env bash
# Restore a backup made by backup.sh:   sudo bash restore.sh /var/lib/spa/backups/spa-YYYY-MM-DD.sql.gz
# The schema must not exist yet (stop the API and `DROP SCHEMA spa CASCADE` first, or restore to a fresh database).
# A schema-only dump does not carry the btree_gist extension that the no-double-booking constraint needs, so it is
# created first; any error aborts and rolls everything back instead of leaving a half-restored schema.
set -euo pipefail
f="${1:?usage: restore.sh <backup.sql.gz>}"
[ -f "$f" ] || { echo "no such file: $f" >&2; exit 1; }
PSQL="${PSQL:-docker exec -i spa-pg psql -U spa -d spa}"   # override to restore into another database
$PSQL -v ON_ERROR_STOP=1 -qc "CREATE EXTENSION IF NOT EXISTS btree_gist"
gunzip -c "$f" | $PSQL -v ON_ERROR_STOP=1 --single-transaction -q
echo "restored $f"
