#!/bin/sh
# Restore (or just TEST) a backup made by ops/backup_postgres.sh.   A backup nobody has restored is only a hope.
#
#   ops/restore_postgres.sh backups/mave-2026-10-04.dump --verify
#       Restores into a scratch database "mave_verify", prints row counts, then drops it. The live database is NOT
#       touched. Run this regularly (e.g. monthly) - it is the only way to know the backups are usable.
#
#   MAVE_RESTORE_CONFIRM=mave ops/restore_postgres.sh backups/mave-2026-10-04.dump
#       REPLACES the live database "mave" with the dump. The api container is stopped meanwhile and started again
#       after a successful restore. If the restore FAILS the api stays stopped on purpose (the database may be half
#       restored): fix the problem, re-run the restore, then `docker compose -f docker-compose.prod.yml start api`.
#       The confirmation variable is required so a typo cannot wipe production.
set -eu
cd "$(dirname "$0")/.."

dump="${1:-}"
mode="${2:-}"
if [ -z "$dump" ] || { [ -n "$mode" ] && [ "$mode" != "--verify" ]; }; then
  echo "usage: $0 backups/mave-YYYY-MM-DD.dump [--verify]" >&2
  exit 2
fi
if [ ! -s "$dump" ]; then
  echo "error: '$dump' does not exist or is empty" >&2
  exit 2
fi

dc() { docker compose -f docker-compose.prod.yml "$@"; }

if [ "$mode" = "--verify" ]; then
  scratch=mave_verify
  cleanup() { dc exec -T db psql -U mave -d postgres -v ON_ERROR_STOP=1 -c "DROP DATABASE IF EXISTS $scratch" >/dev/null 2>&1 || true; }
  trap cleanup EXIT
  dc exec -T db psql -U mave -d postgres -v ON_ERROR_STOP=1 -c "DROP DATABASE IF EXISTS $scratch" >/dev/null
  dc exec -T db psql -U mave -d postgres -v ON_ERROR_STOP=1 -c "CREATE DATABASE $scratch" >/dev/null
  dc exec -T db pg_restore -U mave -d "$scratch" --no-owner --exit-on-error < "$dump"
  for t in users documents sessions; do
    n="$(dc exec -T db psql -U mave -d "$scratch" -At -v ON_ERROR_STOP=1 -c "SELECT count(*) FROM $t")"
    echo "$t: $n rows"
  done
  v="$(dc exec -T db psql -U mave -d "$scratch" -At -v ON_ERROR_STOP=1 -c "SELECT COALESCE(MAX(version), 0) FROM schema_migrations")"
  if [ "${v:-0}" -lt 1 ]; then
    echo "error: restored database has no schema_migrations - not a MAVE backup?" >&2
    exit 1
  fi
  echo "schema version: $v"
  echo "OK: $dump can be restored"
  exit 0
fi

if [ "${MAVE_RESTORE_CONFIRM:-}" != "mave" ]; then
  echo "refusing to replace the live database. Re-run with MAVE_RESTORE_CONFIRM=mave to confirm." >&2
  echo "(use --verify to test a backup without touching the live database)" >&2
  exit 3
fi
trap 'rc=$?; if [ "$rc" -ne 0 ]; then echo "RESTORE FAILED: the api is left stopped and the database may be partly restored. Fix the cause and re-run." >&2; fi' EXIT
dc stop api
dc exec -T db pg_restore -U mave -d mave --clean --if-exists --no-owner --exit-on-error < "$dump"
dc start api
echo "restored $dump into the live database and started the api"
