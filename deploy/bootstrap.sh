#!/usr/bin/env bash
# One-time VPS setup, run as root:  bash bootstrap.sh "<deploy public key>"
#
# Creates the `deploy` user CI connects as, the directories the stacks live
# in, the shared `edge` network, and a firewall that leaves only SSH, HTTP and
# HTTPS open. Safe to run again.
#
# Be clear about what `deploy` is: membership of the docker group is
# root-equivalent on this host. The separate user buys a separate key that
# can be revoked on its own, not a sandbox.
set -euo pipefail

key="${1:?usage: bootstrap.sh \"ssh-ed25519 AAAA... github-actions-deploy\"}"

command -v docker >/dev/null || { echo "Docker is not installed."; exit 1; }
docker compose version >/dev/null || { echo "The docker compose plugin is missing."; exit 1; }
command -v rsync >/dev/null || apt-get install -y rsync

id deploy >/dev/null 2>&1 || adduser --disabled-password --gecos "" deploy
usermod -aG docker deploy
install -d -m 700 -o deploy -g deploy /home/deploy/.ssh
touch /home/deploy/.ssh/authorized_keys
grep -qxF "$key" /home/deploy/.ssh/authorized_keys || echo "$key" >> /home/deploy/.ssh/authorized_keys
chown deploy:deploy /home/deploy/.ssh/authorized_keys
chmod 600 /home/deploy/.ssh/authorized_keys

install -d -o deploy -g deploy /opt/converge-agent /opt/converge-agent/infra /opt/edge
# The compose file mounts it; a missing file would be created as a directory.
# Stays empty when the calendar uses GOOGLE_AUTH_MODE=refresh_token.
f=/opt/converge-agent/google_credentials.json
[ -e "$f" ] || install -m 600 -o deploy -g deploy /dev/null "$f"
docker network inspect edge >/dev/null 2>&1 || docker network create edge

if command -v ufw >/dev/null; then
  ufw allow OpenSSH
  ufw allow 80/tcp
  ufw allow 443/tcp
  ufw allow 443/udp
  ufw --force enable
fi

cat <<'NEXT'

Done. Still to do by hand, once:
  1. /opt/converge-agent/.env                    — the production secrets (never in GitHub)
     /opt/converge-agent/google_credentials.json — if the calendar uses a service account
     /opt/converge-agent/content/staff.json      — the team (not in git; see content/staff.example.json)
     then:  chown deploy:deploy /opt/converge-agent/.env && chmod 600 /opt/converge-agent/.env
  2. /opt/edge: copy deploy/edge/{docker-compose.yml,Caddyfile}, create .env from
     .env.example (ACME_EMAIL, CONVERGE_AGENT_HOST), then: docker compose up -d
     — only once the hostname's DNS points at this server.
NEXT
