#!/bin/bash
# Runs in the server's native image. Only compressed bytes enter the FIFO.
set -euo pipefail
umask 077
runtime="${BACKUP_RUNTIME:-/runtime}"
finish() {
    status=$?
    printf '%s\n' "$status" > "$runtime/status.tmp"
    mv "$runtime/status.tmp" "$runtime/status"
}
trap finish EXIT
export PGCONNECT_TIMEOUT=10
export PGDATABASE=postgres

# Physical backups include all databases, even nonconnectable templates, and
# extension-owned catalogs that a logical dump can omit.
psql -X --quiet --no-password --set=ON_ERROR_STOP=1 --tuples-only --no-align > "$runtime/manifest.json" <<'SQL'
DO $check$
BEGIN
  IF NOT (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) THEN
    RAISE EXCEPTION 'A complete cluster backup requires the approved superuser';
  END IF;
  IF EXISTS (SELECT FROM pg_tablespace WHERE spcname NOT IN ('pg_default', 'pg_global')) THEN
    RAISE EXCEPTION 'Single-stream backups cannot include additional tablespaces';
  END IF;
  IF pg_is_in_recovery() THEN
    RAISE EXCEPTION 'Backups must connect to the primary server';
  END IF;
END $check$;
SELECT json_build_object(
  'formatVersion', 1,
  'method', 'pg_basebackup-tar-wal-fetch',
  'serverVersion', current_setting('server_version'),
  'systemIdentifier', (SELECT system_identifier::text FROM pg_control_system()),
  'startedAt', clock_timestamp(),
  'databases', (SELECT json_agg(datname ORDER BY datname) FROM pg_database),
  'globalsIncluded', true,
  'consistency', 'Whole-cluster physical backup with required recovery WAL',
  'restoreRequirement', 'Matching PostgreSQL major, architecture and installed extension libraries',
  'restoreValidation', 'Not performed by this export'
);
SQL

deadline=$((SECONDS + ${BACKUP_WAIT_SECONDS:-600}))
until [ -p "$runtime/dump.fifo" ]; do
    if [ -e "$runtime/collector-failed" ]; then
        echo 'Backup encryption failed to start' >&2
        exit 1
    fi
    if (( SECONDS >= deadline )); then
        echo 'Backup encryption reader did not start' >&2
        exit 1
    fi
    sleep 1
done
# Keep server messages and SQL containing private data out of pod logs.
if ! pg_basebackup --no-password --pgdata=- --format=tar --wal-method=fetch \
    --checkpoint=spread --manifest-checksums=SHA256 2>"$runtime/dump-error" |
    gzip -c > "$runtime/dump.fifo"; then
    echo 'Whole-server backup failed; backup was not published' >&2
    exit 1
fi

# Database create/drop during the export is not an acceptable complete inventory.
psql -X --quiet --no-password --set=ON_ERROR_STOP=1 --tuples-only --no-align \
    --command="SELECT json_agg(datname ORDER BY datname) FROM pg_database" \
    > "$runtime/databases-after.json"
