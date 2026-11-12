#!/usr/bin/env bash
# Restore a backup made by backup.sh.
#
#   deploy/restore.sh FILE --into NAME   restore into a new database NAME (the live one is untouched)
#   deploy/restore.sh FILE --live        replace the live database: stops the API and worker, restores, starts them
#
# FILE may be "latest" for the newest file in $MERIDIAN_BACKUP_DIR.
source "$(dirname "$0")/lib.sh"
need_passfile
file="${1:?usage: restore.sh FILE|latest --into NAME | --live}"
shift
if [ "$file" = latest ]; then
  file="$(ls -1t "$BACKUP_DIR"/meridian-*.dump.gpg 2>/dev/null | head -1)"
  [ -n "$file" ] || { echo "no backups in $BACKUP_DIR" >&2; exit 1; }
fi
[ -f "$file" ] || { echo "no such file: $file" >&2; exit 1; }
if [ -f "$file.sha256" ]; then
  (cd "$(dirname "$file")" && sha256sum --quiet -c "$(basename "$file").sha256") || { echo "checksum mismatch: $file" >&2; exit 1; }
fi

case "${1:-}" in
  --into)
    target="${2:?--into needs a database name}"
    [ "$target" != "$DB_NAME" ] || { echo "use --live to replace the live database" >&2; exit 1; }
    log "restoring $file into new database $target"
    psql_db postgres -c "DROP DATABASE IF EXISTS \"$target\"" -c "CREATE DATABASE \"$target\" OWNER \"$DB_USER\""
    decrypt "$file" | restore_into "$target"
    log "done: database $target"
    ;;
  --live)
    read -r -p "Replace the live database '$DB_NAME' with $file? Type the database name to confirm: " answer
    [ "$answer" = "$DB_NAME" ] || { echo "cancelled"; exit 1; }
    if [ "$MODE" = docker ]; then
      compose stop api worker
      trap 'compose start api worker' EXIT   # bring the app back even if the restore fails
    fi
    log "restoring $file into $DB_NAME"
    # Keep the current database as <name>_before_restore: block new connections, end open ones, rename.
    psql_db postgres -c "DROP DATABASE IF EXISTS \"${DB_NAME}_before_restore\"" \
      -c "ALTER DATABASE \"$DB_NAME\" ALLOW_CONNECTIONS false" \
      -c "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity WHERE datname = '$DB_NAME' AND pid <> pg_backend_pid()" \
      -c "ALTER DATABASE \"$DB_NAME\" RENAME TO \"${DB_NAME}_before_restore\"" \
      -c "CREATE DATABASE \"$DB_NAME\" OWNER \"$DB_USER\""
    decrypt "$file" | restore_into "$DB_NAME"
    log "done. The previous database is kept as ${DB_NAME}_before_restore. Once you're happy, remove it with:"
    log "  docker compose -f docker-compose.yml -f docker-compose.prod.yml exec db dropdb -U $DB_USER ${DB_NAME}_before_restore"
    ;;
  *)
    echo "usage: restore.sh FILE|latest --into NAME | --live" >&2
    exit 1
    ;;
esac
