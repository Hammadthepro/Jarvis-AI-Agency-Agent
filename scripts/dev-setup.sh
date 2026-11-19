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
if (cd qa-runner && npm ci --no-audit --no-fund >/dev/null && npx playwright install --with-deps chromium >/dev/null 2>&1); then
  touch qa-runner/node_modules/.jarvis-installed; ok "QA runner (Playwright + Chromium)"
else warn "QA runner install failed; dev-up will retry"; fi
