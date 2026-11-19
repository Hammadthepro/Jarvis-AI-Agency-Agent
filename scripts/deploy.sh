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

rsync -az --delete --exclude .git --exclude node_modules --exclude .venv --exclude '.env' --exclude 'env/*.env' \
  -e "ssh -i $VPS_SSH_KEY_FILE -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$KH" ./ "$VPS_USER@$VPS_HOST:/opt/jarvis/"
"${SSH[@]}" bash -se <<REMOTE
set -euo pipefail
cd /opt/jarvis
for f in app runner backup; do test -f env/\$f.env || { echo "env/\$f.env missing on server"; exit 1; }; done
export JARVIS_VERSION=$REV
# Controlled provisioning: images are built here (registry access only at build time). At runtime the sandbox
# runs with --network none and --pull=never; the runner refuses unpinned images in production.
docker compose build migrate
docker build -f docker/qa-sandbox.Dockerfile -t jarvis-qa-sandbox:$REV .
SANDBOX_ID=\$(docker image inspect --format '{{.Id}}' jarvis-qa-sandbox:$REV)
sed -i "s|^SANDBOX_IMAGE=.*|SANDBOX_IMAGE=\$SANDBOX_ID|" env/runner.env
docker compose up -d --remove-orphans migrate sandbox-runner api worker backup caddy
docker compose wait migrate
sleep 10
docker compose exec -T api curl -fsS http://127.0.0.1:8000/ready
docker compose exec -T sandbox-runner curl -fsS http://127.0.0.1:8090/health
REMOTE
echo "deployed $REV"
