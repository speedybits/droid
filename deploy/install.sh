#!/usr/bin/env bash
# Run on the Jetson from the cloned repo: ./deploy/install.sh
# Needs a .env in the repo root (scp it from the laptop; never commit it) with NIMBUS_URL=http://localhost:8080.
# Jetson Nano 2GB: JetPack 4.6 / Ubuntu 18.04. System python is 3.6; python3.10 is built from source in /usr/local/bin
# (same interpreter as ~/projects/nimbus-env), so no uv or extra Python download is needed.
set -euo pipefail
cd "$(dirname "$0")/.."
DIR=$PWD

[ -f .env ] || { echo "missing $DIR/.env (scp it from the laptop)" >&2; exit 1; }
PY=${PYTHON:-/usr/local/bin/python3.10}
[ -x "$PY" ] || { echo "missing $PY (set PYTHON=...)" >&2; exit 1; }

"$PY" -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e .

sed "s|@USER@|$USER|; s|@DIR@|$DIR|" deploy/droid.service | sudo tee /etc/systemd/system/droid.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now droid

# HTTPS for the phone mic; binds to the tailnet only. Skipped (with a hint) until tailscale is installed and logged in.
if command -v tailscale >/dev/null && tailscale status >/dev/null 2>&1; then
  sudo tailscale serve --bg --https=443 http://localhost:8443
  tailscale serve status
else
  echo "tailscale not ready: install it, run 'sudo tailscale up', then: sudo tailscale serve --bg --https=443 http://localhost:8443" >&2
fi

systemctl --no-pager status droid | head -5
