#!/usr/bin/env bash
# Deploy JARVIS API + worker to the VPS over SSH with a PINNED host key (no trust-on-first-use).
# Required env: VPS_HOST, VPS_USER, VPS_SSH_KEY_FILE, VPS_HOST_KEY (the exact known_hosts line, e.g.
# "203.0.113.10 ssh-ed25519 AAAA...") obtained out-of-band from scripts/vps-setup.sh output.
set -euo pipefail
: "${VPS_HOST:?}" "${VPS_USER:?}" "${VPS_SSH_KEY_FILE:?}" "${VPS_HOST_KEY:?pinned host key line required}"
KH="$(mktemp)"; trap 'rm -f "$KH"' EXIT
printf '%s\n' "$VPS_HOST_KEY" > "$KH"
SSH=(ssh -i "$VPS_SSH_KEY_FILE" -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$KH" -o IdentitiesOnly=yes "$VPS_USER@$VPS_HOST")
REV="$(git rev-parse --short HEAD)"

rsync -az --delete --exclude .git --exclude node_modules --exclude .venv --exclude '.env' \
  -e "ssh -i $VPS_SSH_KEY_FILE -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$KH" ./ "$VPS_USER@$VPS_HOST:/opt/jarvis/"
"${SSH[@]}" bash -se <<REMOTE
set -euo pipefail
cd /opt/jarvis
test -f .env || { echo ".env missing on server"; exit 1; }
export JARVIS_VERSION=$REV
docker compose build api
docker build -f docker/qa-sandbox.Dockerfile -t jarvis-qa-sandbox:latest .
docker compose run --rm migrate
docker compose up -d --remove-orphans api worker docker-proxy caddy backup
sleep 5
docker compose exec -T api curl -fsS http://127.0.0.1:8000/ready
REMOTE
echo "deployed $REV"
