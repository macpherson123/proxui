#!/usr/bin/env bash
# Install proxui as a systemd service on the Proxmox host.
# Run as root, from the directory containing px-proxy.py / proxmox-ui.html / proxui.service.
set -euo pipefail

DEST=/opt/proxui
UNIT=/etc/systemd/system/proxui.service

if [[ $EUID -ne 0 ]]; then
  echo "Run as root (sudo ./install.sh)" >&2
  exit 1
fi

SRC="$(cd "$(dirname "$0")" && pwd)"

echo "→ Installing files to $DEST"
mkdir -p "$DEST"
install -m 0644 "$SRC/proxmox-ui.html" "$DEST/proxmox-ui.html"
install -m 0755 "$SRC/px-proxy.py"     "$DEST/px-proxy.py"

echo "→ Installing systemd unit"
install -m 0644 "$SRC/proxui.service" "$UNIT"

echo "→ Stopping any manual instance still holding the port"
pkill -f px-proxy.py 2>/dev/null || true
sleep 1

echo "→ Enabling + starting service"
systemctl daemon-reload
systemctl enable --now proxui.service

sleep 1
systemctl --no-pager --full status proxui.service | head -n 12 || true

# Figure out the scheme the proxy actually came up with.
SCHEME=http
if grep -q 'USE_TLS  = True' "$DEST/px-proxy.py" && [[ -f /etc/pve/local/pve-ssl.pem ]]; then
  SCHEME=https
fi
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"

cat <<EOF

✔ proxui is installed and running.
  Open:        ${SCHEME}://${IP:-<host>}:8090/
  Logs:        journalctl -u proxui -f
  Restart:     systemctl restart proxui
  Update:      re-run ./install.sh  (copies new files + restarts)
  Stop/off:    systemctl disable --now proxui

Note: with TLS on, your browser will warn about Proxmox's self-signed cert
the first time — accept it (same cert the native UI on :8006 uses).
EOF
