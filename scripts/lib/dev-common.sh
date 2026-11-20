# Shared helpers for scripts/dev-*.sh. Sourced, not executed.
# Processes are tracked in .jarvis/run/<name>.pid (each started in its own session, so the whole process group
# can be stopped) and log to .jarvis/logs/<name>.log. Nothing here deletes database volumes or user data.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUN_DIR="$ROOT/.jarvis/run"
LOG_DIR="$ROOT/.jarvis/logs"
COMPOSE=(docker compose -f "$ROOT/docker-compose.dev.yml")
PY="$ROOT/backend/.venv/bin/python"
API_PORT="${JARVIS_API_PORT:-8000}"
WEB_PORT="${JARVIS_WEB_PORT:-5173}"
mkdir -p "$RUN_DIR" "$LOG_DIR"

if [ -t 1 ]; then B=$'\e[1m'; G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; N=$'\e[0m'; else B=""; G=""; Y=""; R=""; N=""; fi
info() { printf '%s\n' "${B}==>${N} $*"; }
ok()   { printf '%s\n' "  ${G}✓${N} $*"; }
warn() { printf '%s\n' "  ${Y}!${N} $*"; }
fail() { printf '%s\n' "  ${R}✗${N} $*" >&2; }
die()  { fail "$*"; exit 1; }

# PID of a managed process if it is alive and really ours (guards against PID reuse), else empty.
managed_pid() {
  local name="$1" f="$RUN_DIR/$1.pid" pid
  [ -f "$f" ] || return 0
  pid="$(cat "$f" 2>/dev/null || true)"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && grep -qa "JARVIS_PROC=$name" "/proc/$pid/environ" 2>/dev/null; then
    echo "$pid"
  else
    rm -f "$f"
  fi
}

# Installs into backend/.venv whether it was created by python -m venv (pip) or by uv (no pip inside).
pip_install() {
  if "$PY" -m pip --version >/dev/null 2>&1; then "$PY" -m pip install -q "$@"
  elif command -v uv >/dev/null; then uv pip install -q --python "$PY" "$@"
  else "$PY" -m ensurepip -q && "$PY" -m pip install -q "$@"; fi
}

port_in_use() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }

# start_proc NAME WORKDIR CMD... : starts once; a second call while it runs is a no-op (no duplicates).
start_proc() {
  local name="$1" dir="$2"; shift 2
  local pid; pid="$(managed_pid "$name")"
  if [ -n "$pid" ]; then ok "$name already running (pid $pid)"; return 0; fi
  : >"$LOG_DIR/$name.log"
  # exec: the recorded PID is the service itself (setsid makes it a session/group leader for clean shutdown).
  # The outer redirection detaches it from the caller's stdout, so callers (and Codespaces hooks) never block on it.
  ( exec 9>&-; cd "$dir" || exit 1; JARVIS_PROC="$name" exec setsid nohup "$@" >>"$LOG_DIR/$name.log" 2>&1 </dev/null ) >/dev/null 2>&1 &
  echo $! >"$RUN_DIR/$name.pid"
  git -C "$ROOT" rev-parse HEAD >"$RUN_DIR/$name.rev" 2>/dev/null || true
  sleep 0.3
  pid="$(cat "$RUN_DIR/$name.pid")"
  kill -0 "$pid" 2>/dev/null || { tail -n 40 "$LOG_DIR/$name.log" >&2; die "$name exited immediately (log: .jarvis/logs/$name.log)"; }
  ok "$name started (pid $pid, log .jarvis/logs/$name.log)"
}

# Restart a running service that was started from different code (e.g. after git pull), so updates take effect.
restart_if_stale() {
  local name="$1" pid cur
  pid="$(managed_pid "$name")"; [ -n "$pid" ] || return 0
  cur="$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"
  if [ "$(cat "$RUN_DIR/$name.rev" 2>/dev/null || echo none)" != "$cur" ]; then
    info "Code changed since $name started: restarting it"
    stop_proc "$name"
  fi
}

stop_proc() {
  local name="$1" pid; pid="$(managed_pid "$name")"
  if [ -z "$pid" ]; then ok "$name not running"; return 0; fi
  kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
  for _ in $(seq 1 40); do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
  if kill -0 "$pid" 2>/dev/null; then warn "$name did not stop in 20s; killing"; kill -KILL -- "-$pid" 2>/dev/null || true; fi
  rm -f "$RUN_DIR/$name.pid"
  ok "$name stopped"
}

# wait_http URL SECONDS [EXPECT_CODE]
wait_http() {
  local url="$1" secs="$2" want="${3:-200}" code
  for _ in $(seq 1 "$secs"); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$url" || true)"
    [ "$code" = "$want" ] && return 0
    sleep 1
  done
  return 1
}

# Value of KEY from the effective JARVIS settings (environment over .env), WITHOUT printing secrets.
setting() { "$PY" -c "import sys; from jarvis.config import get_settings as g; v=getattr(g(), sys.argv[1]); print(v.get_secret_value() if hasattr(v,'get_secret_value') else ('' if v is None else v))" "$1"; }

public_urls() {
  if [ -n "${CODESPACE_NAME:-}" ]; then
    local d="${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-app.github.dev}"
    WEB_URL="https://${CODESPACE_NAME}-${WEB_PORT}.${d}"
    API_URL="https://${CODESPACE_NAME}-${API_PORT}.${d}"
  else
    WEB_URL="http://localhost:${WEB_PORT}"; API_URL="http://localhost:${API_PORT}"
  fi
}
