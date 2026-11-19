#!/usr/bin/env bash
# Keeps the JARVIS voice agent running (started by scripts/dev-up.sh as the managed process "voice").
# Restarts it after a crash with backoff (5s, 10s, 20s, 40s, 60s). If it crashes 5 times within 10 minutes it stops and
# says so, instead of looping forever and hiding the error. Heartbeats to the API are sent by the agent itself.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT/apps/voice"
crashes=()
delay=5
while true; do
  echo "[supervisor] $(date -u +%FT%TZ) starting voice agent"
  "$ROOT/apps/voice/.venv/bin/python" agent.py start
  rc=$?
  now=$(date +%s)
  echo "[supervisor] $(date -u +%FT%TZ) voice agent exited with code $rc"
  crashes+=("$now")
  recent=()
  for t in "${crashes[@]}"; do [ $((now - t)) -lt 600 ] && recent+=("$t"); done
  crashes=("${recent[@]}")
  if [ "${#crashes[@]}" -ge 5 ]; then
    echo "[supervisor] voice agent failed 5 times in 10 minutes; not restarting. Fix the error above, then run scripts/dev-up.sh"
    exit 1
  fi
  echo "[supervisor] restarting in ${delay}s"
  sleep "$delay"
  delay=$(( delay * 2 > 60 ? 60 : delay * 2 ))
done
