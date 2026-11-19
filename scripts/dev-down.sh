#!/usr/bin/env bash
# Stop the JARVIS development processes (dashboard, worker, API). The database keeps running and its data is kept.
#   scripts/dev-down.sh          stop app processes
#   scripts/dev-down.sh --db     also stop the database container (data volume is preserved; never deleted)
set -Eeuo pipefail
source "$(dirname "$0")/lib/dev-common.sh"
cd "$ROOT"
info "Stopping JARVIS"
for p in dashboard worker api; do stop_proc "$p"; done
if [ "${1:-}" = "--db" ]; then
  if command -v docker >/dev/null && docker info >/dev/null 2>&1; then
    "${COMPOSE[@]}" stop db >/dev/null 2>&1 && ok "database container stopped (volume kept)"
  else warn "Docker not available; database not touched"; fi
fi
