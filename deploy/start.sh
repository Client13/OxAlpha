#!/bin/bash
# Runs BOTH components in one Render web service:
#   boterdrop-solver  on 127.0.0.1:8001  (Turnstile tokens, kept alive forever)
#   oxalpha-proxy     on $PORT (public)  (OpenAI-compatible API)
#
# Notes:
# - The proxy binds $PORT immediately (Render's port probe) with
#   --no-autostart so ONLY the keeper below ever launches a browser.
#   (chat.py's Replit autostart would try a second browser and OOM 512MB.)
# - Solver output goes to stdout so failures are visible in Render logs.
set -u

mkdir -p /tmp/logs

# Keeper loop: if the solver process ever exits (browser wedge, OOM), restart it.
(
  cd /app/boterdrop-solver
  while true; do
    echo "[keeper] starting boterdrop-solver..."
    python3 api_server.py 2>&1 | tee -a /tmp/logs/solver.log >&2
    echo "[keeper] $(date -u +%H:%M:%S) solver exited; restarting in 3s" >&2
    sleep 3
  done
) &

# Public API server in the foreground (Render probes $PORT/health)
cd /app/oxalpha-proxy
exec python3 server.py --host 0.0.0.0 --port "${PORT:-10000}" --no-autostart
