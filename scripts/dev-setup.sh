#!/usr/bin/env bash
# One-time setup (Codespace creation/rebuild): create .env if missing and install dependencies.
# Starting the services is scripts/dev-up.sh. Safe to re-run.
set -Eeuo pipefail
source "$(dirname "$0")/lib/dev-common.sh"
cd "$ROOT"
info "Preparing the JARVIS development environment"
[ -f .env ] || { cp .env.example .env && chmod 600 .env && ok "created .env from .env.example"; }
[ -x "$PY" ] || python3 -m venv backend/.venv
pip_install -e "backend[dev]" && touch backend/.venv/.jarvis-installed && ok "backend dependencies"
(cd dashboard && npm ci --no-audit --no-fund >/dev/null) && touch dashboard/node_modules/.jarvis-installed && ok "dashboard dependencies"
# Website QA runs in the isolated Docker sandbox (built and self-checked by dev-up; see scripts/lib/dev-common.sh).
if ensure_qa_sandbox; then ok "website QA $QA_STATUS"; else warn "website QA not ready yet: $QA_STATUS (dev-up retries)"; fi
