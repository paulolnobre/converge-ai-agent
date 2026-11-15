#!/usr/bin/env bash
# One-time on the VPS, as root, after a deploy brought deploy/luks/ to
# /opt/converge-agent/luks/:
#   sudo bash /opt/converge-agent/luks/install.sh
#
# Installs the unlock script and its systemd unit, and enables the unit. Starts
# nothing. Safe to run again.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Rode como root." >&2; exit 1; }
here="$(cd "$(dirname "$0")" && pwd)"
install -m 750 -o root -g root "$here/converge-agent-unlock" /usr/local/sbin/converge-agent-unlock
install -m 644 -o root -g root "$here/converge-agent-volume.service" \
  /etc/systemd/system/converge-agent-volume.service
systemctl daemon-reload
systemctl enable converge-agent-volume.service
echo "Instalado: /usr/local/sbin/converge-agent-unlock e converge-agent-volume.service (habilitado)."
