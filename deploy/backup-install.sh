#!/usr/bin/env bash
# One-time on the VPS, as root, after deploy/backup-aws-setup.sh:
#   bash /opt/converge-agent/backup-install.sh converge-agent ai-agents-construction
#
# Installs restic, a settings file to fill in, the restic repository (once),
# and the systemd timer — one instance per stack in /opt. backup.sh itself
# arrives with every deploy of this repository, in /opt/converge-agent; the
# other stacks are backed up by it too. Safe to run again: it never overwrites
# the settings file or an existing repository.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run as root." >&2; exit 1; }
[ "$#" -gt 0 ] || { echo "usage: backup-install.sh <stack> [stack...]" >&2; exit 2; }
here="$(cd "$(dirname "$0")" && pwd)"

command -v restic >/dev/null || apt-get install -y restic
# --stdin-from-command, which keeps a failed pg_dump from becoming a snapshot.
version="$(restic version | awk '{print $2}')"
printf '%s\n0.17.0\n' "$version" | sort -V -C -r \
  || { echo "restic $version is too old; 0.17 or newer is needed." >&2; exit 1; }

config=/etc/converge-backup.env
if [ ! -e "$config" ]; then
  install -m 640 -o root -g deploy /dev/null "$config"
  cat > "$config" <<'CONF'
# Read by /opt/converge-agent/backup.sh (deploy/backup.sh). root:deploy, 0640.
# From deploy/backup-aws-setup.sh:
RESTIC_REPOSITORY=
AWS_DEFAULT_REGION=sa-east-1
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
# This server's repository password: long and random (openssl rand -base64 32).
# Keep a copy in the password manager — and add your own key too
# (docs/BACKUP.md), so this one can be rotated without losing the backups.
RESTIC_PASSWORD=
# Optional: https://hc-ping.com/<ping-key> — each stack pings <stack>-backup.
BACKUP_PING_URL=
BACKUP_KEEP_DAYS=30
CONF
  echo "Fill in $config, then run this again."
  exit 0
fi

# The repository is created once, as the user the backups run as.
run_restic() { sudo -u deploy bash -c 'set -a; . /etc/converge-backup.env; set +a; exec restic "$@"' _ "$@"; }
if ! run_restic cat config >/dev/null 2>&1; then
  run_restic init
fi

install -m 644 "$here/systemd/converge-backup@.service" "$here/systemd/converge-backup@.timer" \
  /etc/systemd/system/
systemctl daemon-reload
for stack in "$@"; do
  [ -f "/opt/$stack/docker-compose.yml" ] || { echo "No stack in /opt/$stack" >&2; exit 1; }
  systemctl enable --now "converge-backup@$stack.timer"
done
systemctl list-timers 'converge-backup@*' --all
cat <<NEXT

Timers on. Take the first backup now, and check it:
  systemctl start converge-backup@converge-agent.service
  journalctl -u converge-backup@converge-agent.service -n 20
NEXT
