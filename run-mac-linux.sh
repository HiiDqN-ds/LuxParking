#!/usr/bin/env bash
# One-command local start for macOS / Linux:  bash run-mac-linux.sh
set -euo pipefail
cd "$(dirname "$0")"
PY=$(command -v python3 || command -v python) || { echo "Install Python 3.11+ from https://www.python.org/downloads/"; exit 1; }
[ -d .venv ] || "$PY" -m venv .venv
. .venv/bin/activate
pip install -q -r requirements.txt
[ -f .env ] || { cp .env.example .env; echo "Created .env from .env.example: edit passwords in it, then run again."; exit 1; }
set -a; . ./.env; set +a
LAN_IP=$("$PY" lan_ip.py)
[ -n "${QR_BASE_URL:-}" ] || export QR_BASE_URL="http://$LAN_IP:5000"
flask --app wsgi db upgrade
flask --app wsgi seed
echo; echo "On your phone (same Wi-Fi): $QR_BASE_URL"
echo "Website:  http://127.0.0.1:5000/"
echo "Booking:  http://127.0.0.1:5000/reserve/${PUBLIC_TENANT_SLUG:-luxury-parking}/${SEED_LOT_SLUG:-ratingen}"
echo "Admin:    http://127.0.0.1:5000/admin/login   (owner: $SEED_OWNER_EMAIL, platform admin: ${SEED_ADMIN_EMAIL:-none})"; echo
(sleep 2; { command -v open >/dev/null && open http://127.0.0.1:5000/; } || { command -v xdg-open >/dev/null && xdg-open http://127.0.0.1:5000/; } || true) &
exec flask --app wsgi run --debug --host 0.0.0.0 --port 5000
