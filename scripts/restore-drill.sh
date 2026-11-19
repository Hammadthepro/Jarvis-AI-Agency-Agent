#!/usr/bin/env bash
# Monthly restore drill FROM THE OFF-SITE COPY into a scratch database (never production).
# Requires BACKUP_S3_*, BACKUP_AGE_IDENTITY_FILE, RESTORE_DRILL_DATABASE_URL.
set -euo pipefail
exec python -m jarvis.ops.backup restore-drill
