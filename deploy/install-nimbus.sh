#!/usr/bin/env bash
# Run on the Jetson: ./deploy/install-nimbus.sh  (needs sudo). Installs nimbus.service so nimbus starts on boot.
# Assumes nimbus at ~/projects/nimbus with its venv at ~/projects/nimbus-env (see nimbus/CLAUDE.md).
set -euo pipefail
cd "$(dirname "$0")/.."

[ -x "$HOME/projects/nimbus-env/bin/nimbus" ] || { echo "missing ~/projects/nimbus-env/bin/nimbus" >&2; exit 1; }

# A hand-started nimbus would hold port 8080 and the serial port.
pkill -f "nimbus run" || true
sleep 1

sed "s|@USER@|$USER|; s|@HOME@|$HOME|" deploy/nimbus.service | sudo tee /etc/systemd/system/nimbus.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now nimbus

sleep 5
systemctl --no-pager status nimbus | head -5
curl -s localhost:8080/api/health; echo
