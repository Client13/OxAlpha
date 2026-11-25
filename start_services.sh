#!/bin/bash

# Start all 0xalpha services on single port 80
# boterdrop-solver runs internally on 8001, proxy serves on 80
export LD_PRELOAD=/nix/store/sr4cnxyzx24ylxygfk7d81hy4791l8gm-expat-2.7.3/lib/libexpat.so.1
export PYTHONPATH=/home/runner/workspace/.pythonlibs/lib/python3.11/site-packages:$PYTHONPATH
export SOLVER_BASE=http://127.0.0.1:8001
export SOLVER_AUTOSTART=0
export SOLVER_SKIP_DEPS=1
export PORT=80

# Kill any existing processes
pkill -f "api_server.py" 2>/dev/null || true
pkill -f "server.py" 2>/dev/null || true
sleep 1

echo "=== 0xalpha Service Starter ==="
echo "LD_PRELOAD=$LD_PRELOAD"
echo "PORT=$PORT"
echo ""

# Start boterdrop-solver on port 8001 (internal)
echo "Starting boterdrop-solver on port 8001..."
cd /home/runner/workspace/0xalpha/boterdrop-solver
python3 api_server.py > /tmp/solver.log 2>&1 &
SOLVER_PID=$!

# Wait for solver
echo "Waiting for solver..."
for i in $(seq 1 30); do
    if curl -s http://127.0.0.1:8001/health > /dev/null 2>&1; then
        echo "Solver is ready!"
        break
    fi
    if ! kill -0 $SOLVER_PID 2>/dev/null; then
        echo "Solver process died!"
        cat /home/runner/workspace/0xalpha/boterdrop-solver/solver_autostart.log 2>/dev/null || true
        exit 1
    fi
    sleep 2
done

# Start oxalpha-proxy on port 80 (external)
echo "Starting oxalpha-proxy on port 80..."
cd /home/runner/workspace/0xalpha/oxalpha-proxy
python3 server.py --host 0.0.0.0 --port 80 > /tmp/proxy.log 2>&1 &
PROXY_PID=$!

# Wait for proxy
echo "Waiting for proxy..."
for i in $(seq 1 30); do
    if curl -s http://127.0.0.1:80/health > /dev/null 2>&1; then
        echo "Proxy is ready!"
        break
    fi
    if ! kill -0 $PROXY_PID 2>/dev/null; then
        echo "Proxy process died!"
        cat /tmp/proxy.log 2>/dev/null || true
        exit 1
    fi
    sleep 2
done

echo ""
echo "=== All services started on port 80 ==="
echo "API:  http://127.0.0.1/health"
echo "Demo: http://127.0.0.1/"
echo "Models: http://127.0.0.1/v1/models"
echo ""

# Keep running
wait $SOLVER_PID $PROXY_PID
