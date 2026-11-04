#!/usr/bin/env bash
# Render start command: migrate, optionally seed, then serve.
set -euo pipefail
flask --app wsgi db upgrade
if [ "${AUTO_SEED:-0}" = "1" ]; then flask --app wsgi seed; fi
# Threaded workers support Flask-SocketIO's WebSocket transport. Use one process
# unless a shared SOCKETIO_MESSAGE_QUEUE is configured for cross-worker broadcasts.
exec gunicorn wsgi:app --worker-class gthread --threads "${WEB_THREADS:-20}" \
  --bind "0.0.0.0:${PORT:-10000}" --workers "${WEB_CONCURRENCY:-1}" --access-logfile -
