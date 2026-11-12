#!/usr/bin/env bash
# Restore drill: prove the backups work. Takes a fresh backup, restores it into a scratch database, compares
# every table's row count and the schema version with the live database, then drops the scratch copy.
# Exit status 0 means the drill passed. Results are appended to $MERIDIAN_BACKUP_DIR/restore-drills.log.
#
#   deploy/restore-drill.sh          (run monthly; see docs/deployment.md)
source "$(dirname "$0")/lib.sh"
need_passfile
scratch="${DB_NAME}_restore_drill"
started=$(date +%s)

"$HERE/backup.sh"
file="$(ls -1t "$BACKUP_DIR"/meridian-*.dump.gpg | head -1)"
before="$(table_counts "$DB_NAME")"
"$HERE/restore.sh" "$file" --into "$scratch"
after="$(table_counts "$scratch")"
v_live="$(psql_db "$DB_NAME" -At -c 'SELECT version_num FROM alembic_version')"
v_copy="$(psql_db "$scratch" -At -c 'SELECT version_num FROM alembic_version')"
psql_db postgres -c "DROP DATABASE \"$scratch\""

tables=$(echo "$before" | wc -l)
rows=$(echo "$before" | awk '{s += $2} END {print s}')
secs=$(( $(date +%s) - started ))
if [ "$before" = "$after" ] && [ "$v_live" = "$v_copy" ]; then
  result="PASS: $tables tables, $rows rows, schema $v_live, restored from $(basename "$file") in ${secs}s"
  status=0
else
  result="FAIL: restored copy differs from the live database (schema $v_live vs $v_copy)"
  diff <(echo "$before") <(echo "$after") || true
  status=1
fi
log "$result" | tee -a "$BACKUP_DIR/restore-drills.log"
exit $status
