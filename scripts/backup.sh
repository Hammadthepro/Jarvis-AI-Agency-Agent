#!/usr/bin/env bash
# Daily encrypted logical backup of the JARVIS database.
# Supabase Free has no managed backups/PITR, so this is the primary recovery mechanism.
#   - pg_dump (custom format) of DATABASE_URL
#   - encrypted with `age` to BACKUP_AGE_RECIPIENT (private key kept OFFLINE by the owner)
#   - kept locally (7 daily) AND copied off-site to an S3-compatible bucket (e.g. Cloudflare R2) when configured
# Without an off-site destination the backup is reported as BACKUP_NOT_PROTECTED (a local copy on the
# same VPS is not disaster recovery).
set -euo pipefail

: "${DATABASE_URL:?DATABASE_URL is required}"
: "${BACKUP_AGE_RECIPIENT:?BACKUP_AGE_RECIPIENT (age public key) is required}"
DIR="${BACKUP_DIR:-/backups}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$DIR/jarvis-$STAMP.dump.age"
mkdir -p "$DIR"

PGURL="${DATABASE_URL/postgresql+asyncpg/postgresql}"
pg_dump --format=custom --no-owner --no-privileges "$PGURL" | age -r "$BACKUP_AGE_RECIPIENT" > "$OUT"
SHA="$(sha256sum "$OUT" | cut -d' ' -f1)"
SIZE="$(stat -c %s "$OUT")"
OFFSITE=false

if [[ -n "${BACKUP_S3_BUCKET:-}" && -n "${BACKUP_S3_ENDPOINT:-}" ]]; then
  AWS_ACCESS_KEY_ID="$BACKUP_S3_ACCESS_KEY_ID" AWS_SECRET_ACCESS_KEY="$BACKUP_S3_SECRET_ACCESS_KEY" \
    aws --endpoint-url "$BACKUP_S3_ENDPOINT" s3 cp "$OUT" "s3://$BACKUP_S3_BUCKET/$(basename "$OUT")" --only-show-errors
  OFFSITE=true
fi

ls -1t "$DIR"/jarvis-*.dump.age 2>/dev/null | tail -n +8 | while read -r old; do rm -f -- "$old"; done

psql "$PGURL" -v ON_ERROR_STOP=1 -q -c \
  "insert into backup_runs(status, destination, offsite, size_bytes, sha256, finished_at, detail)
   values ('succeeded', '$( [[ $OFFSITE == true ]] && echo "s3://$BACKUP_S3_BUCKET" || echo "local:$DIR")', $OFFSITE, $SIZE, '$SHA', now(),
           '$( [[ $OFFSITE == true ]] && echo ok || echo BACKUP_NOT_PROTECTED)')"
echo "backup ok: $OUT ($SIZE bytes, offsite=$OFFSITE)"
