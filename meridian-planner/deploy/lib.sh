# Shared helpers for the backup scripts. Sourced, not run.
#
# MERIDIAN_BACKUP_MODE=docker (default): talk to the "db" service of the production Compose stack.
# MERIDIAN_BACKUP_MODE=local: use pg_dump/psql on this machine with MERIDIAN_BACKUP_DB_URL (for testing).

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
MODE="${MERIDIAN_BACKUP_MODE:-docker}"
DB_NAME="${MERIDIAN_DB_NAME:-meridian}"
DB_USER="${MERIDIAN_DB_USER:-meridian}"
BACKUP_DIR="${MERIDIAN_BACKUP_DIR:-/srv/backups/meridian}"
PASSFILE="${MERIDIAN_BACKUP_PASSFILE:-$HOME/.config/meridian/backup-passphrase}"
KEEP_DAYS="${MERIDIAN_BACKUP_KEEP_DAYS:-30}"

compose() { (cd "$ROOT" && docker compose -f docker-compose.yml -f docker-compose.prod.yml "$@"); }

# psql_db DBNAME [psql args...]   (reads SQL from stdin or -c)
psql_db() {
  local db="$1"; shift
  if [ "$MODE" = docker ]; then
    compose exec -T db psql -v ON_ERROR_STOP=1 -X -q -U "$DB_USER" -d "$db" "$@"
  else
    psql -v ON_ERROR_STOP=1 -X -q "$(db_url "$db")" "$@"
  fi
}

db_url() {  # local mode: same server as MERIDIAN_BACKUP_DB_URL, another database name
  local base="${MERIDIAN_BACKUP_DB_URL:?set MERIDIAN_BACKUP_DB_URL for local mode}"
  echo "${base%/*}/$1"
}

dump_db() {  # custom-format dump of the live database to stdout
  if [ "$MODE" = docker ]; then
    compose exec -T db pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc
  else
    pg_dump -Fc "$(db_url "$DB_NAME")"
  fi
}

restore_into() {  # restore_into DBNAME  (custom-format dump on stdin)
  if [ "$MODE" = docker ]; then
    compose exec -T db pg_restore -U "$DB_USER" -d "$1" --no-owner --exit-on-error
  else
    pg_restore -d "$(db_url "$1")" --no-owner --exit-on-error
  fi
}

encrypt() { gpg --batch --yes --quiet --pinentry-mode loopback --passphrase-file "$PASSFILE" --symmetric --cipher-algo AES256 -o "$1"; }
decrypt() { gpg --batch --quiet --pinentry-mode loopback --passphrase-file "$PASSFILE" --decrypt "$1"; }

need_passfile() {
  if [ ! -s "$PASSFILE" ]; then
    echo "No backup passphrase at $PASSFILE. Create one (and keep a copy somewhere safe, off this machine):" >&2
    echo "  mkdir -p \"$(dirname "$PASSFILE")\" && openssl rand -base64 32 > \"$PASSFILE\" && chmod 600 \"$PASSFILE\"" >&2
    exit 2
  fi
}

# Row count of every table, sorted: "table count" lines.
table_counts() {
  psql_db "$1" -At -F ' ' <<'SQL'
SELECT format('SELECT %L, count(*) FROM %I.%I', tablename, schemaname, tablename)
FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename
\gexec
SQL
}

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }
