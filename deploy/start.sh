#!/bin/bash
# Runs BOTH components in one Render web service:
#   boterdrop-solver  on 127.0.0.1:8001  (Turnstile tokens, kept alive forever)
#   oxalpha-proxy     on $PORT (public)  (OpenAI-compatible API)
set -u

mkdir -p /tmp/logs

# Keeper loop: if the solver process ever exits (browser wedge, OOM), restart it.
(
  cd /app/boterdrop-solver
  while true; do
    python3 api_server.py >> /tmp/logs/solver.log 2>&1
    echo "[start.sh] $(date -u +%H:%M:%S) solver exited (code $?); restarting in 3s" >> /tmp/logs/solver.log
    sleep 3
  done
) &

# Public API server in the foreground (Render probes $PORT/health)
cd /app/oxalpha-proxy
exec python3 server.py --host 0.0.0.0 --port "${PORT:-10000}"
