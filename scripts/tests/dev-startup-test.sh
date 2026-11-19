#!/usr/bin/env bash
# Integration test of the real development startup workflow (Docker PostgreSQL + dev-up/dev-status/dev-down).
# Uses a THROWAWAY database (jarvis_startup_test) inside the dev container; the developer's `jarvis` database is
# never modified. Stops the running dev services for the duration and restarts them at the end.
#   scripts/tests/dev-startup-test.sh
set -Euo pipefail
source "$(dirname "$0")/../lib/dev-common.sh"
cd "$ROOT"
pass=0; failn=0; Q="'"
check() { if eval "$2"; then ok "$1"; pass=$((pass+1)); else fail "$1"; failn=$((failn+1)); fi; }
TESTDB=jarvis_startup_test
TEST_URL="postgresql://postgres:jarvis@localhost:5432/$TESTDB"
psqlc() { "${COMPOSE[@]}" exec -T db psql -U postgres -d "${2:-$TESTDB}" -tAc "$1"; }
procs() { local n=0 p; for p in /proc/[0-9]*; do grep -qa "JARVIS_PROC=$1" "$p/environ" 2>/dev/null && [ "$(tr '\0' ' ' <"$p/cmdline" | cut -c1-200)" != "" ] && n=$((n+1)); done; echo "$n"; }
nfiles=$(ls database/migrations/*.sql | wc -l)

info "Preparing: stop dev services, create throwaway database"
bash scripts/dev-down.sh >/dev/null
"${COMPOSE[@]}" up -d db >/dev/null 2>&1
for _ in $(seq 1 60); do [ "$(docker inspect -f '{{.State.Health.Status}}' "$("${COMPOSE[@]}" ps -q db)")" = healthy ] && break; sleep 1; done
psqlc "drop database if exists $TESTDB with (force)" postgres >/dev/null
psqlc "create database $TESTDB" postgres >/dev/null
export DATABASE_URL="$TEST_URL"

info "1. First start on an empty database"
out="$(bash scripts/dev-up.sh 2>&1)"; rc=$?
check "dev-up exits 0" '[ $rc = 0 ]'
check "PostgreSQL healthy before migrations" 'grep -q "PostgreSQL container healthy" <<<"$out"'
check "all $nfiles migrations applied" '[ "$(psqlc "select count(*) from schema_migrations")" = "$nfiles" ]'
check "integration_credentials exists" '[ "$(psqlc "select to_regclass(${Q}integration_credentials${Q}) is not null" | tr -d " ")" = t ]'
check "API ready only after migrations" 'curl -sf http://127.0.0.1:$API_PORT/ready | grep -q "\"ok\":true"'
check "API and worker use the same database" '[ "$(psqlc "select count(distinct worker_id) from worker_heartbeats where last_seen_at > now() - interval ${Q}60 seconds${Q}")" -ge 1 ]'
check "dashboard reaches the API through /api" 'curl -sf http://127.0.0.1:$WEB_PORT/api/health >/dev/null'
check "dashboard opens without sign-in (local operator)" 'curl -sf http://127.0.0.1:$WEB_PORT/api/me | grep -q "\"auth_mode\":\"local_development\""'
check "no login page in the served app" '! curl -sf http://127.0.0.1:$WEB_PORT/src/main.tsx | grep -q "pages/Login"'
p_api=$(managed_pid api); p_worker=$(managed_pid worker); p_web=$(managed_pid dashboard)

info "2. Second start: nothing duplicated, migrations not re-run"
out="$(bash scripts/dev-up.sh 2>&1)"; rc=$?
check "second dev-up exits 0" '[ $rc = 0 ]'
check "reports nothing to apply" 'grep -q "nothing (up to date)" <<<"$out"'
check "same API, worker and dashboard processes" '[ "$(managed_pid api)" = "$p_api" ] && [ "$(managed_pid worker)" = "$p_worker" ] && [ "$(managed_pid dashboard)" = "$p_web" ]'
check "exactly one API, worker and dashboard" '[ "$(procs api)" = 1 ] && [ "$(procs worker)" = 1 ] && [ "$(procs dashboard)" -ge 1 ]'
check "schema_migrations unchanged ($nfiles rows)" '[ "$(psqlc "select count(*) from schema_migrations")" = "$nfiles" ]'
check "dev-status reports healthy" 'bash scripts/dev-status.sh >/dev/null 2>&1'

info "3. Existing database from before 0007/0008: pending migrations applied on startup, data kept"
bash scripts/dev-down.sh >/dev/null
psqlc "insert into app_users(id, email) values (gen_random_uuid(), 'keep@startup.test')" >/dev/null
psqlc "drop table voice_agent_heartbeats; drop table integration_status, integration_credentials;
       delete from schema_migrations where filename in ('0007_integration_credentials.sql','0008_voice_agent.sql')" >/dev/null
check "integration_credentials is missing before startup" '[ "$(psqlc "select to_regclass(${Q}integration_credentials${Q}) is null" | tr -d " ")" = t ]'
info "   (a component started without dev-up must refuse to run on the old schema)"
JARVIS_PROC=probe "$ROOT/backend/.venv/bin/uvicorn" jarvis.api.app:get_app --factory --host 127.0.0.1 --port 8765 >"$LOG_DIR/probe-api.log" 2>&1 &
probe=$!; wait_http "http://127.0.0.1:8765/health" 30 >/dev/null
r="$(curl -s -w ' %{http_code}' http://127.0.0.1:8765/ready)"; kill "$probe"; wait "$probe" 2>/dev/null
check "API /ready is 503 and names the pending migrations" 'grep -q "0007_integration_credentials.sql" <<<"$r" && grep -q " 503$" <<<"$r"'
wout="$(timeout 60 "$PY" -m jarvis.worker.main 2>&1)"; wrc=$?
check "worker refuses to start with pending migrations" '[ $wrc != 0 ] && grep -q "migrations are not applied" <<<"$wout"'
out="$(bash scripts/dev-up.sh 2>&1)"; rc=$?
check "dev-up applies 0007 and 0008" '[ $rc = 0 ] && grep -q "0007_integration_credentials.sql" <<<"$out" && grep -q "0008_voice_agent.sql" <<<"$out"'
check "integration_credentials now exists" '[ "$(psqlc "select to_regclass(${Q}integration_credentials${Q}) is not null" | tr -d " ")" = t ]'
check "existing rows preserved" '[ "$(psqlc "select count(*) from app_users where email=${Q}keep@startup.test${Q}")" = 1 ]'

info "4. Configuration errors stop startup with a clear reason"
bash scripts/dev-down.sh >/dev/null
out="$(DATABASE_URL="postgresql://postgres:wrong-password@localhost:5432/$TESTDB" bash scripts/dev-up.sh 2>&1)"; rc=$?
check "wrong password: exit 1, says password authentication failed, no secret printed" '[ $rc = 1 ] && grep -q "password authentication failed" <<<"$out" && ! grep -q "wrong-password" <<<"$out"'
check "wrong password: API not started" '[ -z "$(managed_pid api)" ]'
out="$(DATABASE_URL="postgresql://postgres:jarvis@localhost:5432/no_such_db" bash scripts/dev-up.sh 2>&1)"; rc=$?
check "missing database: exit 1 with a clear message" '[ $rc = 1 ] && grep -q "database does not exist" <<<"$out"'
out="$(JARVIS_ENV=production JARVIS_LOCAL_DEV_ACCESS=true "$PY" -c "from jarvis.config import Settings; Settings()" 2>&1)"; rc=$?
check "production refuses the local access mode" '[ $rc != 0 ] && grep -q "only permitted when JARVIS_ENV=local" <<<"$out"'

info "5. Shutdown"
bash scripts/dev-up.sh >/dev/null 2>&1
bash scripts/dev-down.sh >/dev/null; rc=$?
check "dev-down stops API, worker and dashboard" '[ $rc = 0 ] && [ -z "$(managed_pid api)$(managed_pid worker)$(managed_pid dashboard)" ] && [ "$(procs api)" = 0 ]'
check "database container still running (data kept)" '[ "$(docker inspect -f "{{.State.Running}}" "$("${COMPOSE[@]}" ps -q db)")" = true ]'

info "Cleanup: drop throwaway database, restart dev services"
psqlc "drop database if exists $TESTDB with (force)" postgres >/dev/null
unset DATABASE_URL
bash scripts/dev-up.sh >/dev/null 2>&1 && ok "dev services restarted on the normal database" || warn "restart failed: run scripts/dev-up.sh"
info "Result: $pass passed, $failn failed"
[ "$failn" = 0 ]
