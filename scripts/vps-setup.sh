#!/usr/bin/env bash
# One-time hardening + Docker install for the JARVIS VPS (Ubuntu 24.04). Run as root on a fresh server.
# Does NOT purchase anything and does not create cloud resources.
set -euo pipefail
DEPLOY_USER="${DEPLOY_USER:-jarvis}"
ADMIN_SSH_CIDR="${ADMIN_SSH_CIDR:?set ADMIN_SSH_CIDR to the IP range allowed to SSH, e.g. 203.0.113.4/32}"

apt-get update
apt-get install -y --no-install-recommends ca-certificates curl gnupg ufw unattended-upgrades fail2ban age postgresql-client awscli
dpkg-reconfigure -f noninteractive unattended-upgrades

install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list
apt-get update && apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin

id "$DEPLOY_USER" >/dev/null 2>&1 || useradd -m -s /bin/bash "$DEPLOY_USER"
usermod -aG docker "$DEPLOY_USER"
install -d -m 700 -o "$DEPLOY_USER" "/home/$DEPLOY_USER/.ssh"

# SSH: keys only, no root login
sed -i 's/^#\?PasswordAuthentication .*/PasswordAuthentication no/; s/^#\?PermitRootLogin .*/PermitRootLogin no/' /etc/ssh/sshd_config
systemctl reload ssh || systemctl reload sshd

ufw default deny incoming
ufw default allow outgoing
ufw allow from "$ADMIN_SSH_CIDR" to any port 22 proto tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

install -d -o "$DEPLOY_USER" /opt/jarvis
echo "VPS ready. Next: copy the repo to /opt/jarvis, create /opt/jarvis/.env (mode 600), then run scripts/deploy.sh from CI or locally."
echo "Record this host key fingerprint for deploy pinning:"
ssh-keyscan -t ed25519 localhost 2>/dev/null | ssh-keygen -lf -
