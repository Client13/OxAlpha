#!/usr/bin/env bash
# Start the 0xalpha clean proxy.
#   ./start.sh            -> http://127.0.0.1:8000
#   PORT=9000 ./start.sh
#   PROXY_API_KEY=secret ./start.sh
set -euo pipefail
cd "$(dirname "$0")"
if [ -f .env ]; then set -a; . ./.env; set +a; fi
exec python3 oxalpha_server.py --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}" "$@"
