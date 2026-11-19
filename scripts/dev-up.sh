#!/usr/bin/env bash
# Start JARVIS for development: database -> migrations -> API -> worker -> dashboard, with health checks.
# Safe to run any number of times: running services are left alone, nothing is duplicated, no data is deleted.
#
#   scripts/dev-up.sh            start (or confirm) everything
#   scripts/dev-up.sh --sync     first fast-forward to origin/main if the working tree is clean (used on Codespace start)
#
# Exit codes: 0 all healthy, 1 a step failed (the reason and the relevant log are printed; later steps do not run).
set -Eeuo pipefail
source "$(dirname "$0")/lib/dev-common.sh"
cd "$ROOT"

exec 9>"$RUN_DIR/dev-up.lock"
flock -w 600 9 || die "another dev-up is still running"

SYNC=0
RESTART_API=0
for a in "$@"; do case "$a" in --sync) SYNC=1 ;; *) die "unknown option $a" ;; esac; done

# ---- 0. optional safe sync with GitHub main ----------------------------------------------------------------------------
if [ "$SYNC" = 1 ]; then
  info "Checking for updates on origin/main"
  if [ "$(git rev-parse --abbrev-ref HEAD)" != "main" ]; then warn "not on main; skipping update"
  elif [ -n "$(git status --porcelain)" ]; then warn "uncommitted changes present; not pulling (your work is untouched)"
  elif ! git fetch --quiet origin main; then warn "could not reach GitHub; continuing with the local code"
  elif git merge-base --is-ancestor origin/main HEAD; then ok "up to date with origin/main ($(git rev-parse --short HEAD))"
  elif git merge-base --is-ancestor HEAD origin/main; then git merge --ff-only --quiet origin/main && ok "fast-forwarded to $(git rev-parse --short HEAD)"
  else warn "local main and origin/main have diverged; not changing anything. Resolve manually."
  fi
fi

# ---- 1. configuration ----------------------------------------------------------------------------------------------
info "Configuration"
if [ ! -f .env ]; then
  cp .env.example .env && chmod 600 .env && ok "created .env from .env.example"
else
  ok ".env present (not modified)"
fi
if [ -n "${JARVIS_ENCRYPTION_KEY:-}" ]; then
  ok "JARVIS_ENCRYPTION_KEY provided by the environment (e.g. a Codespaces secret)"
elif grep -qE '^JARVIS_ENCRYPTION_KEY=.+' .env; then
  ok "JARVIS_ENCRYPTION_KEY set in .env"
else
  key="$(openssl rand -base64 32)"
  if grep -qE '^JARVIS_ENCRYPTION_KEY=' .env; then sed -i "s|^JARVIS_ENCRYPTION_KEY=.*|JARVIS_ENCRYPTION_KEY=${key}|" .env
  else printf 'JARVIS_ENCRYPTION_KEY=%s\n' "$key" >>.env; fi
  unset key
  warn "generated JARVIS_ENCRYPTION_KEY once and saved it in .env (never regenerated). To keep it if this"
  warn "Codespace is deleted, copy it into a Codespaces secret named JARVIS_ENCRYPTION_KEY (see docs/DEVELOPMENT.md)."
fi

# Voice agent shared secret: generated once, kept in .env (never printed). The API and the voice agent both read it.
if [ -n "${VOICE_AGENT_SHARED_SECRET:-}" ]; then
  ok "VOICE_AGENT_SHARED_SECRET provided by the environment"
elif grep -qE '^VOICE_AGENT_SHARED_SECRET=.+' .env; then
  ok "VOICE_AGENT_SHARED_SECRET set in .env"
else
  vs="$(openssl rand -hex 32)"
  if grep -qE '^VOICE_AGENT_SHARED_SECRET=' .env; then sed -i "s|^VOICE_AGENT_SHARED_SECRET=.*|VOICE_AGENT_SHARED_SECRET=${vs}|" .env
  else printf 'VOICE_AGENT_SHARED_SECRET=%s\n' "$vs" >>.env; fi
  unset vs
  RESTART_API=1   # a running API must reload settings to know the new secret
  ok "generated VOICE_AGENT_SHARED_SECRET once and saved it in .env"
fi

# ---- 2. dependencies (only when missing or changed) ------------------------------------------------------------------
info "Dependencies"
stamp_ok() { [ -f "$1" ] && [ "$1" -nt "$2" ]; }
if [ ! -x "$PY" ]; then
  python3 -m venv backend/.venv
fi
if ! stamp_ok backend/.venv/.jarvis-installed backend/pyproject.toml; then
  pip_install -e "backend[dev]" >"$LOG_DIR/pip.log" 2>&1 || { tail -n 30 "$LOG_DIR/pip.log" >&2; die "backend install failed"; }
  touch backend/.venv/.jarvis-installed; ok "backend dependencies installed"
else ok "backend dependencies up to date"; fi
if ! stamp_ok dashboard/node_modules/.jarvis-installed dashboard/package-lock.json; then
  (cd dashboard && npm ci --no-audit --no-fund >"$LOG_DIR/npm-dashboard.log" 2>&1) || { tail -n 30 "$LOG_DIR/npm-dashboard.log" >&2; die "dashboard install failed"; }
  touch dashboard/node_modules/.jarvis-installed; ok "dashboard dependencies installed"
else ok "dashboard dependencies up to date"; fi
if ! stamp_ok qa-runner/node_modules/.jarvis-installed qa-runner/package-lock.json; then
  if (cd qa-runner && npm ci --no-audit --no-fund >"$LOG_DIR/npm-qa.log" 2>&1 && npx playwright install chromium >>"$LOG_DIR/npm-qa.log" 2>&1); then
    touch qa-runner/node_modules/.jarvis-installed; ok "QA runner (Playwright + Chromium) installed"
  else warn "QA runner install failed (website QA will report an error until fixed; see .jarvis/logs/npm-qa.log)"; fi
else ok "QA runner dependencies up to date"; fi

# ---- 3. database ----------------------------------------------------------------------------------------------------
info "Database"
DB_URL_HOST="$("$PY" -c "from urllib.parse import urlsplit; from jarvis.config import get_settings; u=urlsplit(get_settings().database_url); print(f'{u.hostname}:{u.port or 5432}')")"
if [ "$DB_URL_HOST" = "localhost:5432" ] || [ "$DB_URL_HOST" = "127.0.0.1:5432" ]; then
  command -v docker >/dev/null || die "Docker is required for the development database (docker-compose.dev.yml)"
  docker info >/dev/null 2>&1 || die "Docker is not running"
  "${COMPOSE[@]}" up -d db >"$LOG_DIR/db-up.log" 2>&1 || { cat "$LOG_DIR/db-up.log" >&2; die "could not start the database container"; }
  cid="$("${COMPOSE[@]}" ps -q db)"
  for i in $(seq 1 60); do
    h="$(docker inspect -f '{{.State.Health.Status}}' "$cid" 2>/dev/null || echo unknown)"
    [ "$h" = "healthy" ] && break
    [ "$i" = 60 ] && { "${COMPOSE[@]}" logs --tail 40 db >&2; die "database did not become healthy in 60s (status: $h)"; }
    sleep 1
  done
  ok "PostgreSQL container healthy (volume preserved)"
else
  ok "using the external database at $DB_URL_HOST from DATABASE_URL"
fi
# Connect exactly as the API and worker will (same settings): wrong password / missing database fail here, clearly.
set +e; out="$("$PY" -m jarvis.db.migrate status 2>&1)"; rc=$?; set -e
case "$rc" in
  0|3) ok "connected with DATABASE_URL" ;;
  *) fail "$out"; die "cannot use the database. Check DATABASE_URL in .env (see docs/DEVELOPMENT.md > Recovering the database)" ;;
esac

# ---- 4. migrations --------------------------------------------------------------------------------------------------
info "Migrations"
set +e; out="$("$PY" -m jarvis.db.migrate 2>&1)"; rc=$?; set -e
[ "$rc" = 0 ] || { fail "$out"; die "a migration failed; services were not started"; }
ok "$out"
out="$("$PY" -m jarvis.db.migrate status 2>&1)" || die "$out"
ok "$out"

# ---- 5. API --------------------------------------------------------------------------------------------------------
info "API"
if [ "$RESTART_API" = 1 ] && [ -n "$(managed_pid api)" ]; then stop_proc api; fi
if [ -z "$(managed_pid api)" ] && port_in_use "$API_PORT"; then die "port $API_PORT is used by a process not started by dev-up; stop it first"; fi
start_proc api "$ROOT" "$ROOT/backend/.venv/bin/uvicorn" jarvis.api.app:get_app --factory --host 127.0.0.1 --port "$API_PORT"
wait_http "http://127.0.0.1:$API_PORT/ready" 60 || { tail -n 40 "$LOG_DIR/api.log" >&2; curl -s "http://127.0.0.1:$API_PORT/ready" >&2 || true; die "API not ready"; }
ok "API ready: database healthy, all migrations applied"

# ---- 6. worker -----------------------------------------------------------------------------------------------------
info "Worker"
start_proc worker "$ROOT" "$PY" -m jarvis.worker.main
for i in $(seq 1 30); do
  [ -n "$(managed_pid worker)" ] || { tail -n 40 "$LOG_DIR/worker.log" >&2; die "worker stopped (see above)"; }
  if "$PY" - <<'EOF' 2>/dev/null; then break; fi
import asyncio, asyncpg, sys
from jarvis.config import get_settings
async def main():
    c = await asyncpg.connect(get_settings().database_url, timeout=5)
    try:
        return await c.fetchval("select exists(select 1 from worker_heartbeats where last_seen_at > now() - interval '60 seconds')")
    finally:
        await c.close()
sys.exit(0 if asyncio.run(main()) else 1)
EOF
  [ "$i" = 30 ] && { tail -n 40 "$LOG_DIR/worker.log" >&2; die "worker did not heartbeat within 30s"; }
  sleep 1
done
ok "worker heartbeating"

# ---- 7. dashboard --------------------------------------------------------------------------------------------------
info "Dashboard"
if [ -z "$(managed_pid dashboard)" ] && port_in_use "$WEB_PORT"; then die "port $WEB_PORT is used by a process not started by dev-up; stop it first"; fi
start_proc dashboard "$ROOT/dashboard" npx vite --port "$WEB_PORT" --strictPort
wait_http "http://127.0.0.1:$WEB_PORT/" 60 || { tail -n 40 "$LOG_DIR/dashboard.log" >&2; die "dashboard not serving"; }
wait_http "http://127.0.0.1:$WEB_PORT/api/health" 20 || die "dashboard cannot reach the API through its /api proxy"
ok "dashboard serving and reaching the API through /api"

# ---- 8. voice agent (only when LiveKit is configured) --------------------------------------------------------------
info "Voice agent"
vstatus="$(curl -s --max-time 10 "http://127.0.0.1:$API_PORT/voice/status" || true)"
lk="$("$PY" -c "import json,sys; print(json.loads(sys.argv[1] or '{}').get('checks',{}).get('livekit_configured', False))" "$vstatus" 2>/dev/null || echo False)"
if [ "$lk" != "True" ]; then
  warn "LiveKit is not configured (Integrations > LiveKit); voice agent not started"
else
  if [ ! -x apps/voice/.venv/bin/python ] || ! stamp_ok apps/voice/.venv/.jarvis-installed apps/voice/requirements.txt; then
    { [ -x apps/voice/.venv/bin/python ] || python3 -m venv apps/voice/.venv; } &&
    (PY="$ROOT/apps/voice/.venv/bin/python"; pip_install -r apps/voice/requirements.txt) >"$LOG_DIR/pip-voice.log" 2>&1 &&
    touch apps/voice/.venv/.jarvis-installed && ok "voice agent dependencies installed" ||
    { tail -n 20 "$LOG_DIR/pip-voice.log" >&2; warn "voice agent install failed (see .jarvis/logs/pip-voice.log)"; }
  else ok "voice agent dependencies up to date"; fi
  if [ -x apps/voice/.venv/bin/python ]; then
    VOICE_AGENT_SHARED_SECRET="$(setting voice_agent_shared_secret)" JARVIS_API_URL="http://127.0.0.1:$API_PORT" \
      start_proc voice "$ROOT" bash "$ROOT/scripts/lib/voice-supervisor.sh"
    online=False
    for _ in $(seq 1 60); do
      online="$(curl -s --max-time 5 "http://127.0.0.1:$API_PORT/voice/status" | "$PY" -c "import json,sys; print(json.load(sys.stdin)['checks']['agent_online'])" 2>/dev/null || echo False)"
      [ "$online" = "True" ] && break
      [ -n "$(managed_pid voice)" ] || break
      sleep 1
    done
    if [ "$online" = "True" ]; then ok "voice agent running and heartbeating (supervised; restarts with backoff)"
    else tail -n 25 "$LOG_DIR/voice.log" >&2; warn "voice agent did not report a heartbeat (see .jarvis/logs/voice.log)"; fi
    vready="$(curl -s --max-time 5 "http://127.0.0.1:$API_PORT/voice/status" | "$PY" -c "import json,sys; d=json.load(sys.stdin); print(d['state'], '|', ' '.join(d['missing']))" 2>/dev/null || true)"
    ok "voice status: ${vready}"
  fi
fi

public_urls
info "JARVIS is running"
printf '  Dashboard: %s\n' "$WEB_URL"
printf '  API:       %s  (private; the dashboard reaches it through /api)\n' "$API_URL"
printf '  Status:    scripts/dev-status.sh    Stop: scripts/dev-down.sh    Logs: .jarvis/logs/\n'
