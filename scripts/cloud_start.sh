#!/usr/bin/env bash
# Container start: the poller in the background, gunicorn in front.
#
# One gunicorn worker, because the run registry (agent/runs.py) is per process
# and the stop button has to find the run it is stopping; threads so `/run`
# polls are not queued behind a slow request. The volume at /data holds the
# database (root, 0600 — set by the app on first open) and one Hermes home per
# user under HERMES_HOMES_DIR (each 0700, owned by its user; the directory
# itself is traversable but not listable).
set -euo pipefail

HOMES="${HERMES_HOMES_DIR:-/data/hermes}"
mkdir -p "$HOMES" "${UPLOADS_DIR:-/data/uploads}"
chmod 0711 "$HOMES"

cd /app
python main.py &
exec gunicorn --bind "0.0.0.0:${PORT:-8000}" --workers 1 --threads 8 --timeout 120 app:app
