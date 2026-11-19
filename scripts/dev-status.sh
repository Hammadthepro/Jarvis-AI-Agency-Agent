#!/usr/bin/env bash
# Report the health of every JARVIS development service. Read-only; safe to run any time.
# Exit 0 when database, migrations, API, worker and dashboard are all healthy, else 1.
set -Euo pipefail
source "$(dirname "$0")/lib/dev-common.sh"
cd "$ROOT"
bad=0
[ -x "$PY" ] || die "backend not installed yet: run scripts/dev-up.sh"

info "Database"
if command -v docker >/dev/null && docker info >/dev/null 2>&1 && cid="$("${COMPOSE[@]}" ps -q db 2>/dev/null)" && [ -n "$cid" ]; then
  ok "container: $(docker inspect -f '{{.State.Status}} / {{.State.Health.Status}}' "$cid")"
else
  warn "development database container not running (or Docker unavailable)"
fi
report="$("$PY" -m jarvis.ops.devstatus 2>&1)" || { fail "$report"; exit 1; }
j() { "$PY" -c "import json,sys; d=json.loads(sys.argv[1]); print(eval(sys.argv[2], {}, {'d': d}))" "$report" "$1"; }
if [ "$(j "d['database'].get('ok')")" = "True" ]; then ok "connected: $(j "d['database']['detail']")"; else fail "$(j "d['database'].get('detail')")"; bad=1; fi

info "Migrations"
if [ "$(j "d['migrations'].get('ok')")" = "True" ]; then ok "all applied ($(j "d['migrations']['applied']"))"
elif [ "$(j "bool(d['migrations'])")" = "True" ]; then fail "pending: $(j "d['migrations']['pending']") modified: $(j "d['migrations']['modified']") (run scripts/dev-up.sh)"; bad=1
else fail "unknown (database unavailable)"; bad=1; fi

info "API"
if [ -n "$(managed_pid api)" ]; then ok "process running (pid $(managed_pid api))"; else fail "process not running"; bad=1; fi
r="$(curl -s --max-time 5 "http://127.0.0.1:$API_PORT/ready" || true)"
if [ -n "$r" ] && curl -sf -o /dev/null --max-time 5 "http://127.0.0.1:$API_PORT/ready"; then ok "/ready: $r"; else fail "/ready: ${r:-no response}"; bad=1; fi

info "Worker"
if [ -n "$(managed_pid worker)" ]; then ok "process running (pid $(managed_pid worker))"; else fail "process not running"; bad=1; fi
if [ "$(j "d['worker'].get('ok')")" = "True" ]; then ok "heartbeat: $(j "d['worker']['detail']")"; else fail "heartbeat: $(j "d['worker'].get('detail', 'unknown')")"; bad=1; fi

info "Dashboard"
if [ -n "$(managed_pid dashboard)" ]; then ok "process running (pid $(managed_pid dashboard))"; else fail "process not running"; bad=1; fi
if curl -sf -o /dev/null --max-time 5 "http://127.0.0.1:$WEB_PORT/"; then ok "serving on port $WEB_PORT"; else fail "not serving on port $WEB_PORT"; bad=1; fi
if curl -sf -o /dev/null --max-time 5 "http://127.0.0.1:$WEB_PORT/api/health"; then ok "reaches the API through /api"; else fail "cannot reach the API through /api"; bad=1; fi

info "Configuration"
ok "JARVIS_ENV=$(j "d['environment']"), local development access: $(j "d['local_dev_access']")"
missing="$(j "'; '.join(d['missing_config'])")"
if [ -n "$missing" ]; then warn "missing: $missing"; else ok "nothing missing for local development"; fi

info "Providers"
if curl -sf -o /dev/null --max-time 5 "http://127.0.0.1:$API_PORT/integrations"; then
  curl -s --max-time 10 "http://127.0.0.1:$API_PORT/integrations" | "$PY" -c "
import json, sys
for i in json.load(sys.stdin)['integrations']:
    print(f\"  {i['name']:<26} {i['state']}\")"
else
  warn "provider states unavailable (API not reachable without a token; local development access off?)"
fi

public_urls
info "URLs"
printf '  Dashboard: %s\n  API:       %s\n' "$WEB_URL" "$API_URL"
exit "$bad"
