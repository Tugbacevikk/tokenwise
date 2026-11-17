#!/bin/sh
# Daily PostgreSQL backups for the hosted Tokenwise (the `backup` service in docker-compose.yml).
#   BACKUP_DIR        where dumps go (default /backups)
#   BACKUP_KEEP_DAYS  how long dumps are kept (default 14)
#   BACKUP_EVERY      seconds between backups (default 86400); 0 = one backup, then exit
# Each dump is checked with pg_restore --list before older ones are deleted, so a broken
# dump never replaces good ones. Restore: see docs/hosting.md, "Yedekleme".
set -eu
dir="${BACKUP_DIR:-/backups}"
keep="${BACKUP_KEEP_DAYS:-14}"
every="${BACKUP_EVERY:-86400}"
mkdir -p "$dir"
while true; do
  name="$dir/tokenwise-$(date -u +%Y%m%d-%H%M%S).dump"
  if pg_dump --format=custom --no-owner --file="$name.partial" "$DATABASE_URL" \
     && pg_restore --list "$name.partial" > /dev/null; then
    mv "$name.partial" "$name"
    find "$dir" -name 'tokenwise-*.dump' -mtime "+$keep" -delete
    echo "backup ok: $name ($(du -h "$name" | cut -f1))"
  else
    rm -f "$name.partial"
    echo "backup FAILED at $(date -u)" >&2
    [ "$every" = 0 ] && exit 1
  fi
  [ "$every" = 0 ] && exit 0
  sleep "$every"
done
