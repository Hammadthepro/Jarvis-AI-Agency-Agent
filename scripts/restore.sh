#!/usr/bin/env bash
# Restore a JARVIS backup into a TARGET database (default: a scratch database for restore drills).
# Usage: scripts/restore.sh <backup.dump.age> <age-identity-file> <target-postgres-url>
# Never point this at production without stopping the worker first.
set -euo pipefail
BACKUP="${1:?backup file}"; IDENTITY="${2:?age identity (private key) file}"; TARGET="${3:?target database url}"
age -d -i "$IDENTITY" "$BACKUP" | pg_restore --no-owner --no-privileges --clean --if-exists -d "$TARGET"
psql "$TARGET" -Atc "select count(*) || ' campaigns, ' || (select count(*) from prospects) || ' prospects restored' from campaigns"
