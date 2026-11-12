#!/usr/bin/env bash
# Encrypted backup of the Meridian database. Run nightly (see meridian-backup.timer).
#
#   deploy/backup.sh            writes $MERIDIAN_BACKUP_DIR/meridian-<UTC time>.dump.gpg (+ .sha256)
#
# The dump is PostgreSQL's custom format, encrypted with AES-256 (gpg, passphrase in $MERIDIAN_BACKUP_PASSFILE).
# Each new file is checked by decrypting it and listing its contents. Files older than $MERIDIAN_BACKUP_KEEP_DAYS
# days (default 30) are deleted.
source "$(dirname "$0")/lib.sh"
need_passfile
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
out="$BACKUP_DIR/meridian-$stamp.dump.gpg"

log "backing up database $DB_NAME ($MODE) to $out"
dump_db | encrypt "$out.partial"
mv "$out.partial" "$out"
chmod 600 "$out"
(cd "$BACKUP_DIR" && sha256sum "$(basename "$out")" > "$(basename "$out").sha256")

# Verify: the file decrypts and is a readable archive with the tables in it.
entries="$(decrypt "$out" | pg_restore --list 2>/dev/null | grep -c 'TABLE DATA' || true)"
if [ "${entries:-0}" -lt 10 ]; then
  log "ERROR: backup verification failed ($entries tables found)"
  exit 1
fi
log "ok: $(du -h "$out" | cut -f1), $entries tables"

find "$BACKUP_DIR" -maxdepth 1 -name 'meridian-*.dump.gpg*' -mtime +"$KEEP_DAYS" -print -delete | sed 's/^/removed old backup: /'
