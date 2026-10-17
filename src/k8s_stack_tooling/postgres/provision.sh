#!/bin/sh
set -eu

# Credentials enter through Secret-backed environment variables, never arguments.
case "${DOCUMENTDB_USER:-}" in
  ''|*[!a-z0-9_]*|[!a-z]*|pg_*|postgres|documentdb|operations_admin|template0|template1)
    echo 'Invalid DocumentDB application user' >&2; exit 1 ;;
esac
[ "${#DOCUMENTDB_USER}" -le 63 ] || exit 1
case "${DOCUMENTDB_DATABASE:-}" in
  ''|*[!a-zA-Z0-9_]*|[!a-zA-Z]*|admin|config|local)
    echo 'Invalid DocumentDB application database' >&2; exit 1 ;;
esac
[ "${#DOCUMENTDB_DATABASE}" -le 63 ] || exit 1
[ "${PGUSER:-}" = documentdb ] || exit 1
[ "${PGDATABASE:-}" = postgres ] || exit 1
[ "${DOCUMENTDB_ADMIN_USER:-}" = operations_admin ] || exit 1
: "${DOCUMENTDB_PASSWORD:?}" "${DOCUMENTDB_ADMIN_PASSWORD:?}"
: "${DOCUMENTDB_HOST:?}" "${DOCUMENTDB_PORT:?}" "${DOCUMENTDB_CA_FILE:?}"

sql() {
  # Driver diagnostics can contain password-bearing SQL; report fixed phases only.
  psql --no-psqlrc --set=ON_ERROR_STOP=1 --quiet "$@" >/dev/null 2>&1
}
mongo() {
  mongosh --host="$DOCUMENTDB_HOST" --port="$DOCUMENTDB_PORT" \
    --tls --tlsCAFile="$DOCUMENTDB_CA_FILE" --quiet --file="$1" >/dev/null 2>&1 &
  mongo_pid=$!
  wait "$mongo_pid"
  mongo_pid=
}

if [ "${1:-}" != --locked ]; then
  deadline=$(($(date +%s) + 180))
  until sql --command='SELECT 1'; do
    if [ "$(date +%s)" -ge "$deadline" ]; then
      echo 'PostgreSQL readiness deadline exceeded' >&2; exit 1
    fi
    sleep 2
  done
  # Keep an instance-wide session lock while the child uses both wire protocols.
  # A failed shell command must also make psql fail, not just print a diagnostic.
  DOCUMENTDB_PROVISIONER=$(realpath "$0")
  export DOCUMENTDB_PROVISIONER
  child_pid_file="${HOME:-/tmp}/documentdb-provisioner.pid"
  stop_provisioner() {
    trap '' 1 2 15
    touch "${HOME:-/tmp}/documentdb-provisioner.cancel"
    if [ -f "$child_pid_file" ]; then
      kill -TERM "$(cat "$child_pid_file")" 2>/dev/null || :
    fi
    # The lock-owning psql must outlive the child's privilege cleanup.
    wait "$lock_pid" || :
    exit 1
  }
  psql --no-psqlrc --set=ON_ERROR_STOP=1 --quiet >/dev/null 2>&1 <<'SQL' &
SET lock_timeout = '180s';
SELECT pg_advisory_lock(hashtextextended('neurwerk-documentdb-provisioning', 0));
\! /bin/sh "$DOCUMENTDB_PROVISIONER" --locked
\if :SHELL_ERROR
SELECT 1 / 0;
\endif
SQL
  lock_pid=$!
  trap stop_provisioner 1 2 15
  if ! wait "$lock_pid"; then
    echo 'DocumentDB provisioning failed' >&2; exit 1
  fi
  echo 'DocumentDB provisioning verified.'
  exit 0
fi

trap 'exit 1' 1 2 15
echo "$$" > "${HOME:-/tmp}/documentdb-provisioner.pid"
[ ! -e "${HOME:-/tmp}/documentdb-provisioner.cancel" ] || exit 1

# Validate an existing SQL identity before changing its password or grants.
DOCUMENTDB_ROLE_EXISTS=$(psql --no-psqlrc --set=ON_ERROR_STOP=1 --quiet --tuples-only --no-align 2>/dev/null <<'SQL'
\getenv application_user DOCUMENTDB_USER
SELECT set_config('addon.documentdb_user', :'application_user', false) \gset
DO $verify$
DECLARE application_user text := current_setting('addon.documentdb_user');
BEGIN
  IF EXISTS (SELECT FROM pg_roles WHERE rolname = application_user AND
      (NOT rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)) OR
     EXISTS (SELECT FROM pg_auth_members m JOIN pg_roles p ON p.oid = m.roleid
       JOIN pg_roles c ON c.oid = m.member JOIN pg_roles g ON g.oid = m.grantor
       WHERE c.rolname = application_user AND
         (p.rolname != 'documentdb_admin_role' OR g.rolname != 'documentdb' OR m.admin_option)) OR
     EXISTS (SELECT FROM pg_auth_members m JOIN pg_roles p ON p.oid = m.roleid
       WHERE p.rolname = application_user) THEN
    RAISE EXCEPTION 'Existing DocumentDB application identity has unexpected privileges';
  END IF;
END $verify$;
SELECT EXISTS (SELECT FROM pg_roles WHERE rolname = :'application_user');
SQL
)
export DOCUMENTDB_ROLE_EXISTS
DOCUMENTDB_PREFLIGHT=true mongo /runtime/provision.js

revoke_authorization() {
  cleanup_status=0
  sql --command='REVOKE ADMIN OPTION FOR documentdb_admin_role FROM operations_admin CASCADE' || cleanup_status=1
  sql --command='ALTER ROLE operations_admin NOCREATEROLE' || cleanup_status=1
  return "$cleanup_status"
}
cleanup() {
  provision_status=$?
  trap - 0
  trap '' 1 2 15
  if [ -n "${mongo_pid:-}" ]; then
    kill -TERM "$mongo_pid" 2>/dev/null || :
    for attempt in 1 2 3 4 5; do
      kill -0 "$mongo_pid" 2>/dev/null || break
      sleep 1
    done
    kill -KILL "$mongo_pid" 2>/dev/null || :
    wait "$mongo_pid" 2>/dev/null || :
  fi
  if ! revoke_authorization; then
    echo 'DocumentDB administrator authorization revocation failed' >&2
    exit 1
  fi
  exit "$provision_status"
}

# Install cleanup before either privilege grant, including partial grant failure.
trap cleanup 0
trap 'exit 1' 1 2 15
sql --command='ALTER ROLE operations_admin CREATEROLE'
sql --command='GRANT documentdb_admin_role TO operations_admin WITH ADMIN OPTION'
mongo /runtime/provision.js

# Regrant as the database owner so CASCADE does not remove product membership.
sql <<'SQL'
\getenv application_user DOCUMENTDB_USER
SELECT format('GRANT documentdb_admin_role TO %I', :'application_user') \gexec
SELECT format('GRANT CONNECT ON DATABASE postgres TO %I', :'application_user') \gexec
SQL
revoke_authorization
trap - 0

sql <<'SQL'
\getenv application_user DOCUMENTDB_USER
SELECT set_config('addon.documentdb_user', :'application_user', false);
DO $verify$
DECLARE application_user text := current_setting('addon.documentdb_user');
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = application_user AND rolcanlogin
      AND NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)) THEN
    RAISE EXCEPTION 'DocumentDB application role attributes failed verification';
  END IF;
  IF NOT has_database_privilege(application_user, 'postgres', 'CONNECT') OR
     NOT has_database_privilege('documentdb_bg_worker_role', 'postgres', 'CONNECT') OR
     EXISTS (SELECT FROM pg_database WHERE datname NOT IN ('postgres', 'template0') AND
       (has_database_privilege(application_user, oid, 'CONNECT') OR
        has_database_privilege('documentdb_bg_worker_role', oid, 'CONNECT'))) THEN
    RAISE EXCEPTION 'DocumentDB PostgreSQL isolation failed verification';
  END IF;
  IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'operations_admin' AND rolcreaterole) OR
     NOT EXISTS (SELECT FROM pg_auth_members m JOIN pg_roles p ON p.oid = m.roleid
       JOIN pg_roles c ON c.oid = m.member WHERE p.rolname = 'documentdb_admin_role'
       AND c.rolname = 'operations_admin' AND NOT m.admin_option) THEN
    RAISE EXCEPTION 'DocumentDB administrator revocation failed verification';
  END IF;
  IF NOT EXISTS (SELECT FROM pg_auth_members m JOIN pg_roles p ON p.oid = m.roleid
       JOIN pg_roles c ON c.oid = m.member JOIN pg_roles g ON g.oid = m.grantor
       WHERE p.rolname = 'documentdb_admin_role' AND c.rolname = application_user
       AND g.rolname = 'documentdb' AND NOT m.admin_option) OR
     EXISTS (SELECT FROM pg_auth_members m JOIN pg_roles p ON p.oid = m.roleid
       JOIN pg_roles c ON c.oid = m.member WHERE c.rolname = application_user
       AND (p.rolname != 'documentdb_admin_role' OR m.admin_option)) THEN
    RAISE EXCEPTION 'DocumentDB application memberships failed verification';
  END IF;
END $verify$;
SQL
mongo /runtime/verify.js
PGPASSWORD="$DOCUMENTDB_PASSWORD" PGUSER="$DOCUMENTDB_USER" sql --command='SELECT current_user'
