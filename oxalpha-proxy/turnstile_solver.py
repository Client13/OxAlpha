#!/usr/bin/env python3
"""
Client for a Boterdrop-Solver instance (najibyahya/Boterdrop-Solver).

That project exposes an async task API learned from its README + api_server.py:

    GET /turnstile?url=<page>&sitekey=<key>&action=<action>  -> 202 {task_id}
    GET /result?id=<task_id>  -> 202 (still working) | 200 {status, value, ...}

This module speaks that protocol with stdlib only, so server.py / chat.py can
automatically satisfy oxalpha.com's `turnstile_required` challenge instead of
failing the request:

    token = solve_turnstile("http://127.0.0.1:8001",
                            "https://oxalpha.com/chat/",
                            "0x4AAAAAABp_UYRVl7snkQ51", action="chat")

Why these parameters: oxalpha.com renders its Turnstile widget with
`action: 'chat'` (see /chat inline script), so the token must be minted for
the same sitekey + action + page URL — the solver stubs that exact page in
its browser and clicks the widget there.

You must run the solver yourself (it needs a real browser + RAM):
    cd boterdrop-solver && python3 api_server.py   # default port 8001
Then point at it:  SOLVER_BASE=http://127.0.0.1:8001
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request


class SolverError(Exception):
    def __init__(self, message, code="solver_error"):
        super().__init__(message)
        self.message = message
        self.code = code


def _get_json(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": "oxalpha-proxy/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8", "replace") or "{}")
        except Exception:
            body = {"message": f"solver HTTP {e.code}"}
        return e.code, body


def solve_turnstile(base, url, sitekey, action="chat", timeout=150, poll_interval=2.0):
    """Mint a Turnstile token via the solver. Returns the token string.

    Raises SolverError if the solver is down, rejects the task, or is too slow.
    """
    base = base.rstrip("/")
    qs = urllib.parse.urlencode({"url": url, "sitekey": sitekey, "action": action})
    try:
        status, created = _get_json(f"{base}/turnstile?{qs}", timeout=30)
    except Exception as e:
        raise SolverError(
            f"solver unreachable at {base} ({e}). Start it: "
            f"cd boterdrop-solver && python3 api_server.py",
            "solver_down") from e
    task_id = created.get("task_id") if isinstance(created, dict) else None
    if status not in (200, 202) or not task_id:
        detail = (created.get("error") or created.get("message")
                  if isinstance(created, dict) else created)
        raise SolverError(f"solver refused task (HTTP {status}): {detail}",
                          "task_rejected")

    deadline = time.time() + timeout
    poll_url = f"{base}/result?id=" + urllib.parse.quote(task_id)
    while True:
        if time.time() > deadline:
            raise SolverError(
                f"solver timed out after {timeout}s on task {task_id[:8]}…",
                "timeout")
        # poll interval also bounds request rate at ~1 per poll_interval
        time.sleep(poll_interval)
        status, res = _get_json(poll_url, timeout=30)
        if status == 202 or (status == 200 and isinstance(res, dict)
                             and res.get("status") == "process"):
            continue  # still solving — keep polling
        if status == 404:
            raise SolverError("solver forgot the task (expired?), retry the request",
                              "task_expired")
        if status == 408 or (isinstance(res, dict) and res.get("value") == "timeout"):
            raise SolverError("solver gave up (timeout inside solver)", "timeout")
        if status == 200 and isinstance(res, dict) and res.get("status") == "success":
            token = res.get("value", "")
            if token and token not in ("captcha_fail", "timeout"):
                return token
            raise SolverError(f"solver failed to mint a token: {res}", "captcha_fail")
        detail = res if not isinstance(res, dict) else (
            res.get("message") or res.get("value") or res.get("error") or res)
        raise SolverError(f"solver error (HTTP {status}): {detail}", "solver_error")


def solver_health(base, timeout=10):
    """Best-effort health probe. Returns dict; never raises."""
    try:
        status, body = _get_json(base.rstrip("/") + "/health", timeout=timeout)
        if status == 200 and isinstance(body, dict):
            return body
        return {"status": "unknown", "note": f"HTTP {status} (older solver without /health?)"}
    except Exception as e:
        return {"status": "down", "note": str(e)[:200]}
