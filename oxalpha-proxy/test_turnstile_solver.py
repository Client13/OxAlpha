#!/usr/bin/env python3
"""Tests for turnstile_solver.py against a stub Boterdrop-Solver HTTP server.

Covers the protocol learned from the repo: 202 task accept -> 202 polling ->
200 success, plus the error paths (refused task, solver-side failure,
timeout, unreachable solver).

  python3 test_turnstile_solver.py
"""
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import turnstile_solver as ts

MODE = {"name": "ok", "polls": 0, "seen": []}


class Stub(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/turnstile":
            MODE["seen"].append(q)
            if MODE["name"] == "refuse":
                self._send(400, {"status": "error", "error": "bad sitekey"})
            else:
                self._send(202, {"task_id": "test-task-1", "status": "accepted"})
        elif u.path == "/result":
            MODE["polls"] += 1
            if MODE["name"] == "ok":
                if MODE["polls"] < 3:
                    self._send(202, {"status": "process", "message": "solving turnstile"})
                else:
                    self._send(200, {"status": "success", "elapsed_time": 2.4,
                                     "value": "TOKEN123"})
            elif MODE["name"] == "fail":
                self._send(422, {"status": "error", "elapsed_time": 5.0,
                                 "value": "captcha_fail"})
            elif MODE["name"] == "timeout":
                self._send(200, {"status": "process", "message": "solving"})
        elif u.path == "/health":
            self._send(200, {"status": "ok", "pool": 2, "capacity": 2,
                             "browser_connected": True})
        else:
            self._send(404, {"status": "error", "message": "nope"})


srv = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_port}"
passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name}")
    else:
        failed += 1
        print(f"  FAIL {name}")


# 1. happy path: accept -> 2x processing -> token
MODE.update(name="ok", polls=0, seen=[])
tok = ts.solve_turnstile(BASE, "https://oxalpha.com/chat/",
                         "0x4AAAAAABp_UYRVl7snkQ51", action="chat", timeout=30)
check("happy path returns token", tok == "TOKEN123")
sent = MODE["seen"][0]
check("sends url+sitekey+action",
      sent.get("url") == ["https://oxalpha.com/chat/"]
      and sent.get("sitekey") == ["0x4AAAAAABp_UYRVl7snkQ51"]
      and sent.get("action") == ["chat"])
check("polled until done", MODE["polls"] >= 3)

# 2. refused task
MODE.update(name="refuse", polls=0, seen=[])
try:
    ts.solve_turnstile(BASE, "https://oxalpha.com/chat/", "bad", timeout=10)
    check("refused task raises", False)
except ts.SolverError as e:
    check("refused task raises", e.code == "task_rejected")

# 3. solver-side captcha failure
MODE.update(name="fail", polls=0, seen=[])
try:
    ts.solve_turnstile(BASE, "https://oxalpha.com/chat/", "k", timeout=10)
    check("captcha_fail raises", False)
except ts.SolverError as e:
    check("captcha_fail raises", True)

# 4. client-side timeout while solver still working
MODE.update(name="timeout", polls=0, seen=[])
try:
    ts.solve_turnstile(BASE, "https://oxalpha.com/chat/", "k", timeout=5,
                       poll_interval=1.0)
    check("timeout raises", False)
except ts.SolverError as e:
    check("timeout raises", e.code == "timeout")

# 5. unreachable solver
try:
    ts.solve_turnstile("http://127.0.0.1:1", "https://oxalpha.com/chat/", "k",
                       timeout=5)
    check("unreachable raises helpful", False)
except ts.SolverError as e:
    check("unreachable raises helpful", "Start it" in e.message)

# 6. health probe (ok + down, never raises)
check("health ok", ts.solver_health(BASE).get("status") == "ok")
check("health down", ts.solver_health("http://127.0.0.1:1").get("status") == "down")

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
