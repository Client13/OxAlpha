#!/bin/bash
# Runs BOTH components in one Render web service:
#   solver-lite (UI bridge) on 127.0.0.1:8001  — drives the real oxalpha chat
#   oxalpha-proxy            on $PORT (public) — OpenAI-compatible API
#
# Notes:
# - The proxy binds $PORT immediately (Render's port probe) with
#   --no-autostart (no solver autostart; the keeper below owns the browser).
# - SOLVER_IMPL=boterdrop switches back to the og Camoufox solver (fat hosts
#   only — Camoufox OOM-kills 512MB containers at spawn).
# - Both components log to stdout so failures are visible in Render logs.
set -u

mkdir -p /tmp/logs /tmp/lite-profile

# Virtual display for headed Chromium (patchright stealth requirement)
Xvfb :99 -screen 0 1280x800x24 > /dev/null 2>&1 &
export DISPLAY=:99

SOLVER_IMPL="${SOLVER_IMPL:-lite}"

if [ "$SOLVER_IMPL" = "boterdrop" ]; then
  (
    cd /app/boterdrop-solver
    while true; do
      echo "[keeper] starting boterdrop-solver..."
      python3 api_server.py 2>&1 | tee -a /tmp/logs/solver.log >&2
      echo "[keeper] $(date -u +%H:%M:%S) solver exited; restarting in 3s" >&2
      sleep 3
    done
  ) &
else
  (
    cd /app/solver-lite
    while true; do
      echo "[keeper] starting solver-lite (UI bridge)..."
      python3 api_server_lite.py 2>&1 | tee -a /tmp/logs/solver.log >&2
      echo "[keeper] $(date -u +%H:%M:%S) solver-lite exited; restarting in 3s" >&2
      sleep 3
    done
  ) &
fi

# Public API server in the foreground (Render probes $PORT/health)
cd /app/oxalpha-proxy
exec python3 server.py --host 0.0.0.0 --port "${PORT:-10000}" --no-autostart
