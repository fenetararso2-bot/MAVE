#!/bin/sh
# Daily PostgreSQL backup for the docker-compose.prod.yml stack (custom-format dump, 14 copies kept).
#   crontab:  17 3 * * *  cd /srv/mave && ./ops/backup_postgres.sh
# Restore:    docker compose -f docker-compose.prod.yml exec -T db pg_restore -U mave -d mave --clean --if-exists < backups/mave-YYYY-MM-DD.dump
# Copy the backups directory off the server too - a backup on the same disk is not a backup.
set -eu
cd "$(dirname "$0")/.."
mkdir -p backups
out="backups/mave-$(date +%F).dump"
docker compose -f docker-compose.prod.yml exec -T db pg_dump -U mave -Fc mave > "$out.tmp"
mv "$out.tmp" "$out"
ls -1t backups/mave-*.dump | tail -n +15 | xargs -r rm --
echo "backup written: $out"
