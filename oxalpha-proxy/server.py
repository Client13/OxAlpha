"""
OxAlpha -> OpenAI-compatible proxy.

Routes OpenAI chat requests to the free web chat backend at
https://oxalpha.com/chat (POST https://oxalpha.com/api/chat).

Reverse-engineered flow (2026-09-05, from /chat HTML + /chatapp/chat-app.min.js):
  1. GET /chat sets cookies `ox_alpha_session` (HttpOnly) + `XSRF-TOKEN`
     and returns HTML containing:
       <meta name="csrf-token" content="...">
       window.__CHAT_MODELS__ = [{"id":"z-ai/glm-5.3-flash","slug":"ox-alpha",...}]
       window.__TURNSTILE_SITE_KEY__ = "0x4AAAAAABp_UYRVl7snkQ51"
  2. POST /api/chat with:
       Headers: Content-Type: application/json
                X-CSRF-TOKEN: <meta content>
                Cookie: <both cookies>
                Origin: https://oxalpha.com
                Referer: https://oxalpha.com/chat
                User-Agent: <browser UA>
                (optional) X-Turnstile-Token: <token> on retry after 428
       Body: {"model":"z-ai/glm-5.3-flash",
              "messages":[{"role":"user","content":"hi"}, ...]}
     Only `model` + `messages[{role,content:string}]` are sent by the web app.
  3. Response is `text/event-stream`, an OpenRouter passthrough:
       : OPENROUTER PROCESSING          <- keep-alive comments while queued
       data: {"id":"gen-...","object":"chat.completion.chunk","model":"z-ai/glm-5.3-flash",
              "provider":"Z.AI",
              "choices":[{"index":0,"delta":{"content":"...","role":"assistant"},
                          "finish_reason":null}]}
       ...
       data: {...,"choices":[{"delta":{"content":"","role":"assistant"},
              "finish_reason":"stop"}],"usage":{...}}}
       data: [DONE]
     Errors may arrive as HTTP non-2xx JSON ({error, code}) or as
     `event: error` + `data: {"status":429,...}` inside the stream.
     When abuse protection triggers: HTTP 428 or 200 JSON
       {"error":"Please complete the quick verification...","code":"turnstile_required",...}
     The browser then shows a Cloudflare Turnstile modal and retries with
     X-Turnstile-Token. This proxy can solve that automatically IF you run a
     Boterdrop-Solver instance (see ../boterdrop-solver, run api_server.py)
     and set SOLVER_BASE — it mints a token for the oxalpha sitekey and the
     request is retried with X-Turnstile-Token, exactly like the browser does.
     Without SOLVER_BASE (or with a client-supplied X-Turnstile-Token header)
     the challenge is surfaced as HTTP 429 instead.

Exposed surface:
  GET  /                      -> built-in demo chat app (uses the API below)
  GET  /health                -> {status, upstream, model, solver}
  GET  /v1/models
  POST /v1/chat/completions      (also /chat/completions alias)
       body: {model, messages, stream?, tools?, temperature?, max_tokens?, ...}
       - `model` accepts ox-alpha | stealth/ox-alpha | z-ai/glm-5.3-flash
         (anything else is mapped to the upstream default but echoed back).
       - `messages` content may be string or OpenAI content-parts list;
         text parts are concatenated, image_url parts are dropped with a warning
         (the web JSON path is text-only).
       - `tools` (OpenAI function-calling) is bridged: schemas become a strict
         JSON reply protocol in the prompt, and protocol replies come back as
         real assistant `tool_calls` (finish_reason "tool_calls"); assistant
         tool_calls / tool results in history are rendered back to plain text.
       - `stream: true`  -> SSE passthrough, `data: [DONE]` terminated.
       - `stream: false` (default) -> buffered JSON chat.completion. Empty or
         reasoning-only upstream output is retried internally and answered
         with a normal non-empty message (or 502 after the retries), never
         with an empty `content`.

Stdlib only (no pip install). Python 3.11+.

  python3 server.py [--port 8000] [--host 127.0.0.1]

Env:
  PORT=8000  PROXY_API_KEY=...  (if set, require `Authorization: Bearer ...`)
  UPSTREAM_BASE=https://oxalpha.com  (override for debugging)
  SOLVER_BASE=http://127.0.0.1:8001  (default ON — a local Boterdrop-Solver
    is auto-launched if none answers, so Turnstile never blocks the demo.
    Empty string disables: SOLVER_BASE=)
  SOLVER_TIMEOUT=150  SOLVER_ACTION=chat

IMPORTANT - use responsibly:
  oxalpha.com is a free, rate-limited demo (observed: ~30 req/min headers,
  plus IP-based daily/anti-bot limits + Turnstile). Scraping it as a production
  agent backend is fragile and likely against its Terms. For production use the
  official OpenAI-compatible routes instead: OpenRouter `stealth/ox-alpha`,
  Tokenra, or AIMLAPI (see README). This proxy is for personal research /
  low-volume prototyping only.
"""

from __future__ import annotations

import argparse
import http.client
import http.server
import json
import os
import re
import socketserver
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

UPSTREAM_BASE = os.environ.get("UPSTREAM_BASE", "https://oxalpha.com")
UPSTREAM_HOST = urllib.parse.urlparse(UPSTREAM_BASE).hostname or "oxalpha.com"
UPSTREAM_MODEL = "z-ai/glm-5.3-flash"
MODEL_ALIASES = {"ox-alpha", "stealth/ox-alpha", "z-ai/glm-5.3-flash"}
BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
SESSION_TTL = 10 * 60  # refresh cookies+csrf every 10 min

# Turnstile auto-solve (Boterdrop-Solver protocol, see turnstile_solver.py).
# The sitekey/action/page must match how oxalpha.com renders its widget
# (inline script on /chat: sitekey below, action 'chat').
TURNSTILE_SITEKEY = "0x4AAAAAABp_UYRVl7snkQ51"
TURNSTILE_URL = "https://oxalpha.com/chat/"
SOLVER_BASE = os.environ.get("SOLVER_BASE", "http://127.0.0.1:8001").rstrip("/")
SOLVER_TIMEOUT = int(os.environ.get("SOLVER_TIMEOUT", "150"))
SOLVER_ACTION = os.environ.get("SOLVER_ACTION", "chat")
# Transport: "api" = oxalpha /api/chat + Turnstile auto-solve (og behavior,
# needs a browser in the solver); "ui" = solver-lite drives the REAL web app
# (new chat -> paste -> send -> read reply), which needs no Turnstile token at
# all and survives on small hosts like Render free tier.
UPSTREAM_MODE = os.environ.get("UPSTREAM_MODE", "api")
# Server-side guardrails (Render/oreo hardening):
MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", str(8 * 1024 * 1024)))
# Whole-request ceiling for buffered completions. oreo's provider aborts at
# 180s, so retries must fit under this deadline or be surfaced as a retryable
# 503 (which oreo re-drives with its own backoff) instead of hanging to the abort.
REQUEST_DEADLINE = float(os.environ.get("REQUEST_DEADLINE", "165"))
# Streaming passthrough ceiling: a stalled upstream (queued forever) ends as an
# explicit error chunk + [DONE] instead of an eternal keep-alive ping loop.
STREAM_MAX_SECONDS = float(os.environ.get("STREAM_MAX_SECONDS", "150"))

try:
    from turnstile_solver import SolverError, solve_turnstile, solver_health
except ImportError:  # running without the helper module next to server.py
    SolverError = Exception  # type: ignore

    def solve_turnstile(*a, **k):
        raise SolverError("turnstile_solver.py not found next to server.py")

    def solver_health(base, timeout=10):
        return {"status": "unknown"}

# ---------------------------------------------------------------- session ---
_session: dict[str, Any] = {"cookies": {}, "csrf": "", "at": 0.0}


def _parse_set_cookies(headers: list[tuple[str, str]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in headers:
        if k.lower() != "set-cookie":
            continue
        pair = v.split(";", 1)[0]
        if "=" in pair:
            name, val = pair.split("=", 1)
            out[name.strip()] = val.strip()
    return out


def _cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def upstream_init_session(force: bool = False) -> dict[str, Any]:
    """GET /chat -> fresh cookies + csrf token. Cached for SESSION_TTL."""
    global _session
    if not force and _session["csrf"] and time.time() - _session["at"] < SESSION_TTL:
        return _session
    url = UPSTREAM_BASE + "/chat"
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw_headers = [(k, v) for k, v in resp.headers.items()]
        # get_all for Set-Cookie (urllib collapses; use get_all if available)
        try:
            all_set = resp.headers.get_all("Set-Cookie") or []
        except Exception:
            all_set = []
        cookies = _parse_set_cookies([(k, v) for k, v in raw_headers] + [("Set-Cookie", c) for c in all_set])
        html = resp.read().decode("utf-8", "replace")
    m = re.search(r'<meta name="csrf-token" content="([^"]+)"', html)
    csrf = m.group(1) if m else ""
    if not csrf:
        raise UpstreamError(502, {
            "error": "session init failed: /chat page carried no csrf token "
                     "(Cloudflare challenge on this IP, or upstream changed)",
            "code": "session_init_failed",
        })
    _session = {"cookies": cookies, "csrf": csrf, "at": time.time()}
    return _session


# --------------------------------------------------------------- upstream ---
class UpstreamError(Exception):
    def __init__(self, status: int, payload: Any):
        super().__init__(str(payload)[:500])
        self.status = status
        self.payload = payload


def upstream_ui_text(messages: list[dict], timeout: int = 120) -> str:
    """Chat through the solver-lite UI bridge; returns the assistant reply."""
    body = json.dumps({"messages": messages, "timeout": max(20, min(timeout, 150))}).encode()
    req = urllib.request.Request(SOLVER_BASE.rstrip("/") + "/chat", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout + 15) as r:
            data = json.loads(r.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace") or "{}"
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {"error": raw[:300]}
        raise UpstreamError(e.code if e.code in (429, 502, 504) else 502, payload)
    except Exception as e:
        raise UpstreamError(502, {"error": f"solver chat unreachable: {e}"})
    text = str(data.get("text", ""))
    if not text.strip():
        raise UpstreamError(502, {"error": "solver returned empty text"})
    return text


def _ui_sse_iter(text: str):
    """Synthesize OpenRouter-style SSE lines from a full UI reply so the
    streaming and buffered consumers need no changes."""
    created = int(time.time())
    uid = f"chatcmpl-ui-{created}"

    def chunk(delta, finish=None):
        return "data: " + json.dumps({"id": uid, "object": "chat.completion.chunk",
                                      "created": created,
                                      "choices": [{"index": 0, "delta": delta,
                                                   "finish_reason": finish}]})
    buf = ""
    for part in re.findall(r"\S+\s*", text):
        buf += part
        if len(buf) >= 24:
            yield chunk({"content": buf})
            buf = ""
    if buf:
        yield chunk({"content": buf})
    yield chunk({}, finish="stop")
    yield "data: [DONE]"


def upstream_chat_stream(
    messages: list[dict],
    turnstile_token: str | None = None,
    timeout: int = 120,
):
    """POST /api/chat (api mode) or the UI bridge (ui mode), yield raw SSE
    lines (str, without trailing newline). Raises UpstreamError on failures.
    Caller must iterate to completion; connection closed at the end."""
    if UPSTREAM_MODE == "ui":
        return _ui_sse_iter(upstream_ui_text(messages, timeout)), {}
    sess = upstream_init_session()
    body = json.dumps({"model": UPSTREAM_MODEL, "messages": messages}).encode()
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "X-CSRF-TOKEN": sess["csrf"],
        "Origin": UPSTREAM_BASE,
        "Referer": UPSTREAM_BASE + "/chat",
        "User-Agent": BROWSER_UA,
        "Cookie": _cookie_header(sess["cookies"]),
    }
    if turnstile_token:
        headers["X-Turnstile-Token"] = turnstile_token

    def _do_once(hdrs: dict) -> http.client.HTTPResponse:
        ctx = ssl.create_default_context()
        conn = http.client.HTTPSConnection(UPSTREAM_HOST, 443, timeout=timeout, context=ctx)
        conn.request("POST", "/api/chat", body=body, headers=hdrs)
        return conn.getresponse()

    def _is_turnstile_err(e: UpstreamError) -> bool:
        p = e.payload if isinstance(e.payload, dict) else {}
        return e.status == 428 or str(p.get("code", "")) == "turnstile_required"

    def _auto_solve() -> str:
        """Mint a token via Boterdrop-Solver. Raises UpstreamError if it fails."""
        try:
            return solve_turnstile(SOLVER_BASE, TURNSTILE_URL, TURNSTILE_SITEKEY,
                                   action=SOLVER_ACTION, timeout=SOLVER_TIMEOUT)
        except Exception as se:
            msg = getattr(se, "message", str(se))[:500]
            raise UpstreamError(429, {
                "code": "turnstile_required",
                "error": f"Turnstile auto-solve failed: {msg}. Run the solver "
                         f"(cd boterdrop-solver && python3 api_server.py, port 8001) "
                         f"with SOLVER_BASE set, pass X-Turnstile-Token manually, "
                         f"or retry later.",
            })

    def _post_checked(hdrs: dict) -> http.client.HTTPResponse:
        resp = _do_once(hdrs)
        # CSRF rotation: refresh once on 419
        if resp.status == 419:
            resp.close()
            sess = upstream_init_session(force=True)
            hdrs["X-CSRF-TOKEN"] = sess["csrf"]
            hdrs["Cookie"] = _cookie_header(sess["cookies"])
            resp = _do_once(hdrs)

        if resp.status in (428, 403, 429, 401, 402, 408, 422, 500, 502, 503):
            raw = resp.read().decode("utf-8", "replace")
            resp.close()
            try:
                payload = json.loads(raw) if raw else {"error": f"upstream {resp.status}"}
            except Exception:
                payload = {"error": raw[:1000]}
            raise UpstreamError(resp.status, payload)

        if resp.status != 200:
            raw = resp.read().decode("utf-8", "replace")
            resp.close()
            # Some Turnstile challenges come back as 200 + JSON (not SSE)
            ctype = resp.getheader("Content-Type", "")
            if "application/json" in ctype:
                try:
                    raise UpstreamError(resp.status, json.loads(raw))
                except UpstreamError:
                    raise
                except Exception:
                    pass
            raise UpstreamError(resp.status, {"error": raw[:1000]})

        ctype = resp.getheader("Content-Type", "")
        # Defensive: upstream sometimes answers 200+JSON (turnstile_required)
        if "application/json" in ctype:
            raw = resp.read().decode("utf-8", "replace")
            resp.close()
            try:
                payload = json.loads(raw)
            except Exception:
                payload = {"error": raw[:1000]}
            raise UpstreamError(429 if payload.get("code") == "turnstile_required" else 502, payload)
        return resp

    try:
        resp = _post_checked(headers)
    except UpstreamError as e:
        # No client token + solver configured -> solve & retry once, like the
        # site's own fetch wrapper does with its Turnstile modal.
        if _is_turnstile_err(e) and not turnstile_token and SOLVER_BASE:
            headers["X-Turnstile-Token"] = _auto_solve()
            # fresh session too: the challenge may have been session-scoped
            sess = upstream_init_session(force=True)
            headers["X-CSRF-TOKEN"] = sess["csrf"]
            headers["Cookie"] = _cookie_header(sess["cookies"])
            try:
                resp = _post_checked(headers)
            except UpstreamError as e2:
                if _is_turnstile_err(e2):
                    p = dict(e2.payload) if isinstance(e2.payload, dict) else {}
                    p["error"] = ("auto-solve failed: solved token rejected by upstream "
                                  "(invalid/expired, or solver IP is limited too). "
                                  + str(p.get("error", "")))
                    raise UpstreamError(e2.status, p)
                raise
        elif not _is_turnstile_err(e) and e.status == 429:
            # Overloaded (not a challenge): one short retry for the demo.
            time.sleep(5)
            resp = _post_checked(headers)
        else:
            raise

    # Stream SSE lines
    def gen():
        buf = b""
        try:
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    yield line.decode("utf-8", "replace").rstrip("\r")
        finally:
            resp.close()

    return gen(), dict(resp.getheaders())


# ---------------------------------------------------------------- helpers ---
def flatten_content(content: Any) -> tuple[str, bool]:
    """OpenAI content (str | parts[]) -> plain text. Returns (text, had_image)."""
    if content is None:
        return "", False
    if isinstance(content, str):
        return content, False
    if isinstance(content, list):
        texts: list[str] = []
        had_image = False
        for part in content:
            if not isinstance(part, dict):
                texts.append(str(part))
                continue
            t = part.get("type")
            if t == "text":
                texts.append(str(part.get("text", "")))
            elif t in ("image_url", "image"):
                had_image = True
            elif "text" in part:
                texts.append(str(part["text"]))
        return "\n".join(texts), had_image
    return str(content), False


# ----------------------------------------------------------- tool bridge -----
# oxalpha.com only accepts plain text turns, but OpenAI clients (Oreo included)
# speak function-calling. The bridge translates in both directions: tool
# schemas become a strict reply-JSON protocol in the prompt, and a protocol
# reply becomes real OpenAI tool_calls on the way back.

def tools_catalog(tools: Any) -> list[dict]:
    """Normalize an OpenAI `tools` array into [{name, description, params}]."""
    out: list[dict] = []
    if not isinstance(tools, list):
        return out
    for t in tools:
        fn = t.get("function") if isinstance(t, dict) else None
        if isinstance(fn, dict) and fn.get("name"):
            params = fn.get("parameters")
            out.append({
                "name": str(fn["name"]),
                "description": str(fn.get("description") or "")[:400],
                "params": params if isinstance(params, dict) else {},
            })
    return out


def tool_protocol(catalog: list[dict]) -> str:
    rows = []
    for t in catalog:
        try:
            schema = json.dumps(t["params"], separators=(",", ":"))
        except Exception:
            schema = "{}"
        rows.append("- %s: %s | args schema: %s" % (t["name"], t["description"] or "no description", schema[:600]))
    return (
        "TOOL ACCESS. You can run real tools this turn. Reply with EXACTLY ONE JSON object and nothing else:\n"
        'To run tools: {"calls":[{"name":"<tool name>","args":{...}}]} — one or more calls; '
        'the app executes them and sends back "Tool result" messages.\n'
        'To reply without running tools: {"answer":"<your message>"}\n'
        "Rules: raw JSON only (no prose, no markdown fences, no explanation before or after), "
        "use tool names from this list only, keep args valid for the schema.\n"
        "Tools:\n" + "\n".join(rows)
    )


def _json_objects(raw: str):
    """Yield balanced top-level {...} substrings (string-aware, fence-tolerant)."""
    depth = 0
    start = None
    in_str = False
    esc = False
    for i, ch in enumerate(raw):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    yield raw[start:i + 1]
                    start = None


def repair_json(text: str, closer: str = "close") -> str | None:
    """Best-effort structural repair of near-JSON protocol output (stdlib only).

    `closer` picks the strategy for a mismatched closing bracket:
      - "close": close the interrupted contexts down to the nearest matching opener
        (recovers a dropped brace mid-list);
      - "drop": ignore the stray closer and keep scanning (recovers over-closed
        output without losing the trailing array elements).
    parse_tool_reply tries both and keeps the richer interpretation.

    The upstream model occasionally emits the tool protocol with small structural
    slips that strict json.loads rejects. Two failure modes are observed:
      1. a dropped closing brace mid-structure, e.g. ...{"name":"plan","args":{...}},
         {"name":"search",... where one '}' is missing — the array element above
         swallows the rest and the tail closers no longer line up;
      2. output truncated before the closing brackets (or mid-string).
    The scanner tracks object/array contexts and, on a mismatched closer or a
    value where a key was expected, closes the interrupted object(s) first.
    Returns repaired text, or None when nothing sensible can be made of it.
    """
    if not text or len(text) > 400_000:
        return None
    stack: list[list] = []  # entries: ['{', state] or ['[', state]; state in 'key|colon|value|comma'
    out: list[str] = []
    in_str = False
    esc = False

    def state() -> str | None:
        return stack[-1][1] if stack else None

    def set_state(v: str) -> None:
        if stack:
            stack[-1][1] = v

    def trim_comma() -> None:
        while out and out[-1] in " \t\r\n":
            out.pop()
        if out and out[-1] == ",":
            out.pop()

    def close_through(kind: str) -> bool:
        """Close every context above the nearest open `kind` ('{' or '[')."""
        for depth in range(len(stack) - 1, -1, -1):
            if stack[depth][0] == kind:
                while len(stack) > depth + 1:
                    opener, _ = stack.pop()
                    trim_comma()
                    out.append("}" if opener == "{" else "]")
                return True
        return False

    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
                st = state()
                if st == "key":
                    set_state("colon")
                elif st in ("value", "comma"):
                    set_state("comma")
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch.isspace():
            out.append(ch)
            i += 1
            continue
        st = state()
        if ch == "{":
            if st in ("key", "colon"):
                # A value starts where a key/colon was expected: the previous
                # element's object is missing its closing brace (observed bug).
                trim_comma()
                out.append("}")
                stack.pop()
                if state() == "value":
                    set_state("comma")
                out.append(",")
                if stack:
                    stack[-1][1] = "comma"
                stack.append(["{", "key"])
                out.append(ch)
            else:
                stack.append(["{", "key"])
                out.append(ch)
        elif ch == "[":
            if st in ("key", "colon"):
                trim_comma()
                out.append("}")
                stack.pop()
                out.append(",")
                if stack:
                    stack[-1][1] = "comma"
                stack.append(["[", "value"])
                out.append(ch)
            else:
                stack.append(["[", "value"])
                out.append(ch)
        elif ch == "}":
            if stack and stack[-1][0] == "{" and st in ("comma", "key", "value"):
                trim_comma()
                out.append(ch)
                stack.pop()
                if not stack:
                    return "".join(out)  # root object complete; ignore trailing closers
                if state() == "value":
                    set_state("comma")
            elif stack and stack[-1][0] == "[":
                if closer == "drop":
                    i += 1
                    continue  # stray closer: ignore it and keep scanning
                # mismatched '}' while inside an array: close down to nearest object
                if not close_through("{"):
                    return None
                trim_comma()
                out.append(ch)
                stack.pop()
                if state() == "value":
                    set_state("comma")
            else:
                return None
        elif ch == "]":
            if stack and stack[-1][0] == "[" and st in ("comma", "value"):
                trim_comma()
                out.append(ch)
                stack.pop()
                if state() == "value":
                    set_state("comma")
            elif stack and stack[-1][0] == "{":
                if closer == "drop":
                    i += 1
                    continue  # stray closer: ignore it and keep scanning
                # mismatched ']' while inside an object: close down to nearest array
                if not close_through("["):
                    return None
                trim_comma()
                out.append(ch)
                stack.pop()
                if state() == "value":
                    set_state("comma")
            else:
                return None
        elif ch == ",":
            if st in ("comma", "value", "key"):
                out.append(ch)
                if stack:
                    stack[-1][1] = "value" if stack[-1][0] == "[" else "key"
            else:
                return None
        elif ch == ":":
            if st == "colon":
                out.append(ch)
                set_state("value")
            elif st == "key":
                out.append(ch)  # tolerate missing quotes edge; keep scanning
                set_state("value")
            else:
                return None
        else:
            # bare scalar characters (number / true / false / null fragment)
            if st in ("key", "colon") and ch not in '-0123456789tfn':
                return None
            out.append(ch)
            if st == "key":
                if stack:
                    stack[-1][1] = "colon"
            elif st == "colon":
                if stack:
                    stack[-1][1] = "value"
            elif st == "comma" and stack and stack[-1][0] == "[":
                stack[-1][1] = "value"
        i += 1

    if in_str:
        # Truncated mid-string: close it so the tail stays parseable.
        if esc and out and out[-1] == "\\":
            out.pop()
        out.append('"')
        st = state()
        if st == "key":
            if stack:
                stack[-1][1] = "colon"
        elif st == "colon":
            out.append("null")
            if stack:
                stack[-1][1] = "comma"
        elif st == "value" and stack and stack[-1][0] == "{":
            stack[-1][1] = "comma"
    while stack:
        opener, _ = stack.pop()
        trim_comma()
        out.append("}" if opener == "{" else "]")
    return "".join(out)


def _scan_protocol(scan: str, names: set[str]) -> dict | None:
    """Extract {'calls': [...]} or {'content': answer} from scan text, or None."""
    for cand in _json_objects(scan):
        try:
            obj = json.loads(cand)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        if "calls" not in obj and "tool_calls" not in obj and "answer" not in obj:
            continue
        calls = obj.get("calls", obj.get("tool_calls"))
        if isinstance(calls, dict):
            calls = [calls]
        parsed = []
        for c in calls if isinstance(calls, list) else []:
            if not isinstance(c, dict):
                continue
            fn = c.get("function") if isinstance(c.get("function"), dict) else {}
            name = fn.get("name") or c.get("name") or c.get("tool")
            args = fn.get("arguments")
            if args is None:
                args = c.get("args", c.get("arguments", c.get("parameters")))
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {}
            if not isinstance(args, dict):
                args = {}
            if isinstance(name, str) and name in names:
                parsed.append({"name": name, "args": args})
        if parsed:
            return {"calls": parsed}
        answer = obj.get("answer")
        if isinstance(answer, str) and answer.strip():
            return {"content": answer}
        break
    return None


def parse_tool_reply(text: str, catalog: list[dict]) -> dict:
    """Translate a model reply into {'calls': [{name,args}...]} or {'content': str}."""
    raw = (text or "").strip()
    if not raw:
        return {"content": raw}
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
    scan = fenced.group(1) if fenced else raw
    names = {t["name"] for t in catalog}
    # First pass: strict JSON candidates as-is. Second pass: the same text after
    # structural repair — the upstream sometimes drops a closing brace mid-list
    # or truncates the tail, and those replies must still become real tool calls
    # instead of leaking raw protocol JSON into the chat.
    first = _scan_protocol(scan, names)
    if first:
        return first
    # Structural repair, both interpretations: "close" recovers a dropped brace,
    # "drop" recovers over-closed output. Prefer the candidate with more tool calls
    # so extra closers never cost the trailing calls.
    best = None
    for mode in ("close", "drop"):
        alt = repair_json(scan, mode)
        if not alt or alt == scan:
            continue
        result = _scan_protocol(alt, names)
        if not result:
            continue
        if "calls" in result:
            if best is None or "calls" not in best or len(result["calls"]) > len(best["calls"]):
                best = result
        elif best is None:
            best = result
    return best or {"content": raw}


def openai_error(handler: http.server.BaseHTTPRequestHandler, status: int, message: str, code: str = "upstream_error", retry_after: int | None = None):
    payload = {"error": {"message": message, "type": code, "code": code}}
    data = json.dumps(payload).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(data)))
    if retry_after:
        handler.send_header("Retry-After", str(retry_after))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    handler.wfile.write(data)


def send_mapped_error(handler, e: "UpstreamError"):
    """Map an UpstreamError and attach a Retry-After on transient statuses so
    retrying clients (oreo included) back off politely instead of hammering."""
    st, msg, code = map_upstream_error(e)
    ra = 5 if st == 429 else (3 if st in (500, 502, 503, 504) else None)
    return openai_error(handler, st, msg, code, retry_after=ra)


def map_upstream_error(e: UpstreamError) -> tuple[int, str, str]:
    p = e.payload if isinstance(e.payload, dict) else {"error": str(e.payload)}
    code = str(p.get("code", ""))
    msg = str(p.get("error", p.get("message", e.payload)))[:2000]
    if code == "turnstile_required" or e.status == 428:
        # (auto-solve failures already carry their own guidance in msg)
        hint = ("" if "auto-solve failed" in msg else
                "Run a Boterdrop-Solver and set SOLVER_BASE for auto-solve, or pass "
                "a manually solved token via X-Turnstile-Token, or retry later. ")
        return 429, (
            "Upstream oxalpha.com asked for human verification (Cloudflare Turnstile). "
            + hint +
            f"Upstream said: {msg}"
        ), "turnstile_required"
    if e.status == 429 or "overload" in msg.lower() or "limit" in msg.lower():
        return 429, f"Upstream overloaded/rate-limited: {msg}", "rate_limit_exceeded"
    if e.status == 422 or "Invalid model" in msg:
        return 400, f"Upstream rejected request: {msg}", "invalid_request_error"
    if e.status in (401, 403):
        return 502, f"Upstream config/auth error: {msg}", "upstream_error"
    if e.status == 408:
        return 504, f"Upstream timed out: {msg}", "timeout"
    return 502, f"Upstream error ({e.status}): {msg}", "upstream_error"


# ---------------------------------------------------------------- demo -----
DEMO_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ox Alpha — demo chat</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; font-family: system-ui, sans-serif; background: #111; color: #eee;
         display: flex; flex-direction: column; height: 100vh; }
  header { padding: 10px 14px; background: #1b1b1b; display: flex; gap: 10px;
           align-items: center; border-bottom: 1px solid #333; }
  header b { flex: 1; }
  #badge { font-size: 12px; padding: 3px 10px; border-radius: 10px; background: #333; }
  #badge.ok { background: #14532d; } #badge.bad { background: #7f1d1d; }
  #log { flex: 1; overflow-y: auto; padding: 14px; display: flex; flex-direction: column; gap: 10px; }
  .msg { max-width: 80%; padding: 9px 12px; border-radius: 10px; white-space: pre-wrap; word-wrap: break-word; }
  .user { align-self: flex-end; background: #1d4ed8; }
  .bot { align-self: flex-start; background: #222; border: 1px solid #333; }
  .err { align-self: center; background: #7f1d1d; font-size: 13px; }
  form { display: flex; gap: 8px; padding: 12px; border-top: 1px solid #333; }
  input { flex: 1; padding: 10px; border-radius: 8px; border: 1px solid #444; background: #1b1b1b; color: #eee; }
  button { padding: 10px 16px; border-radius: 8px; border: 0; background: #1d4ed8; color: #fff; cursor: pointer; }
  button:disabled { opacity: .5; }
</style>
</head>
<body>
<header><b>Ox Alpha — demo chat</b><span id="badge">solver: …</span>
<button id="new" type="button">New chat</button></header>
<div id="log"></div>
<form id="f"><input id="in" autocomplete="off" placeholder="Type a message…">
<button id="send" type="submit">Send</button></form>
<script>
const log = document.getElementById('log'), badge = document.getElementById('badge');
let history = [];
fetch('health').then(r => r.json()).then(h => {
  const s = (h.solver && h.solver.status) || 'unknown';
  badge.textContent = 'solver: ' + s;
  badge.className = s === 'ok' ? 'ok' : (s === 'disabled' || s === 'down' ? 'bad' : '');
}).catch(() => { badge.textContent = 'solver: ?'; });
function add(cls, text) {
  const d = document.createElement('div'); d.className = 'msg ' + cls; d.textContent = text;
  log.appendChild(d); log.scrollTop = log.scrollHeight; return d;
}
document.getElementById('new').onclick = () => { history = []; log.innerHTML = ''; };
document.getElementById('f').onsubmit = async (e) => {
  e.preventDefault();
  const inp = document.getElementById('in'), btn = document.getElementById('send');
  const text = inp.value.trim(); if (!text) return;
  inp.value = ''; btn.disabled = true;
  add('user', text);
  history.push({role: 'user', content: text});
  const bubble = add('bot', '…');
  try {
    const r = await fetch('v1/chat/completions', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({model: 'ox-alpha', messages: history, stream: true})
    });
    if (!r.ok) {
      let msg = 'HTTP ' + r.status;
      try { msg = (await r.json()).error.message || msg; } catch (_) {}
      bubble.remove(); add('err', msg); history.pop(); return;
    }
    const reader = r.body.getReader(), dec = new TextDecoder();
    let buf = '', out = '';
    bubble.textContent = '';
    for (;;) {
      const {done, value} = await reader.read();
      if (done) break;
      buf += dec.decode(value, {stream: true});
      const parts = buf.split('\\n'); buf = parts.pop();
      for (const line of parts) {
        const t = line.trim();
        if (!t.startsWith('data:')) continue;
        const data = t.slice(5).trim();
        if (data === '[DONE]') break;
        try {
          const c = JSON.parse(data).choices[0].delta.content;
          if (c) { out += c; bubble.textContent = out; log.scrollTop = log.scrollHeight; }
        } catch (_) {}
      }
    }
    history.push({role: 'assistant', content: out});
  } catch (err) { bubble.remove(); add('err', String(err)); history.pop(); }
  btn.disabled = false;
};
</script>
</body>
</html>
"""


# ---------------------------------------------------------------- server ----
class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "OxAlphaProxy/1.2"
    timeout = 300  # socket guard: a wedged peer can never leak a thread forever

    def log_message(self, fmt, *args):
        pass  # quiet; change to print for debugging

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*, Authorization, X-Turnstile-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def _need_auth(self) -> bool:
        want = os.environ.get("PROXY_API_KEY", "")
        if not want:
            return True
        auth = self.headers.get("Authorization", "")
        return auth == f"Bearer {want}"

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/chat", "/demo", "/index.html"):
            data = DEMO_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self._cors()
            self.end_headers()
            self.wfile.write(data)
        elif path == "/health":
            health: dict[str, Any] = {"status": "ok", "upstream": UPSTREAM_BASE,
                                      "model": UPSTREAM_MODEL, "mode": UPSTREAM_MODE}
            if SOLVER_BASE:
                health["solver"] = {"base": SOLVER_BASE,
                                    **solver_health(SOLVER_BASE)}
            else:
                health["solver"] = {"status": "disabled",
                                    "note": "set SOLVER_BASE to enable Turnstile auto-solve"}
            data = json.dumps(health).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self._cors()
            self.end_headers()
            self.wfile.write(data)
        elif path in ("/v1/models", "/models"):
            now = int(time.time())
            data = json.dumps({
                "object": "list",
                "data": [
                    {"id": "ox-alpha", "object": "model", "created": now, "owned_by": "oxalpha",
                     "upstream": UPSTREAM_MODEL},
                    {"id": "stealth/ox-alpha", "object": "model", "created": now, "owned_by": "oxalpha",
                     "upstream": UPSTREAM_MODEL},
                    {"id": UPSTREAM_MODEL, "object": "model", "created": now, "owned_by": "oxalpha",
                     "upstream": UPSTREAM_MODEL},
                ],
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self._cors()
            self.end_headers()
            self.wfile.write(data)
        else:
            openai_error(self, 404, f"unknown route {path}", "invalid_request_error")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path not in ("/v1/chat/completions", "/chat/completions", "/v1/completions"):
            return openai_error(self, 404, f"unknown route {path}", "invalid_request_error")
        if not self._need_auth():
            return openai_error(self, 401, "invalid API key", "invalid_request_error")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length > MAX_BODY_BYTES:
            return openai_error(self, 413, f"request body too large (limit {MAX_BODY_BYTES} bytes)", "invalid_request_error")
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return openai_error(self, 400, "invalid JSON body", "invalid_request_error")

        req_model = str(req.get("model", "ox-alpha"))
        stream = bool(req.get("stream", False))
        turnstile = self.headers.get("X-Turnstile-Token") or req.get("turnstile_token")

        raw_msgs = req.get("messages", [])
        if not isinstance(raw_msgs, list) or not raw_msgs:
            return openai_error(self, 400, "`messages` must be a non-empty array", "invalid_request_error")
        catalog = tools_catalog(req.get("tools"))
        messages: list[dict] = []
        dropped_images = False
        for m in raw_msgs:
            if not isinstance(m, dict) or ("content" not in m and not m.get("tool_calls")):
                return openai_error(self, 400, "each message needs {role, content}", "invalid_request_error")
            text, had_img = flatten_content(m.get("content"))
            dropped_images = dropped_images or had_img
            role = m.get("role", "user")
            if role not in ("system", "user", "assistant", "developer", "tool"):
                role = "user"
            if role == "developer":
                role = "system"
            # OpenAI tool plumbing the upstream cannot represent. Assistant
            # turns that were pure tool calls are dropped (they carry no voice,
            # and echoing "Called tool(...)" records makes models mimic that
            # style in their answers); the paired tool results become a plain
            # "Tool result" user turn so the model still sees the full loop.
            if role == "assistant" and m.get("tool_calls") and not text.strip():
                continue
            if role == "tool":
                role = "user"
                text = "Tool result: " + text
            messages.append({"role": role, "content": text})

        # The web chat upstream only accepts user/assistant turns ("Invalid messages"
        # on any system role). Fold every system prompt into the opening user turn
        # instead of dropping it, so agent frameworks that lead with a system prompt
        # (Oreo, OpenAI SDK clients, ...) still deliver their instructions.
        sys_txt = "\n\n".join(m["content"] for m in messages if m["role"] == "system").strip()
        if catalog:
            # Function-calling bridge: the tool schemas ride along with the
            # folded system text (the upstream has no `tools` parameter).
            sys_txt = (sys_txt + "\n\n" if sys_txt else "") + tool_protocol(catalog)
        if sys_txt:
            rest = [m for m in messages if m["role"] != "system"]
            first_user = next((i for i, m in enumerate(rest) if m["role"] == "user"), None)
            if first_user is None:
                rest = [{"role": "user", "content": sys_txt}] + rest
            else:
                lead = rest[first_user]
                lead["content"] = sys_txt + "\n\n---\n\n" + (lead["content"] or "")
            messages = rest
        # Consecutive user turns (a request followed by its tool results) read
        # as one turn; the upstream web app prefers strictly alternating roles.
        merged: list[dict] = []
        for m in messages:
            if m["role"] == "user" and merged and merged[-1]["role"] == "user":
                merged[-1]["content"] = (merged[-1]["content"] + "\n\n" + m["content"]).strip()
            else:
                merged.append(dict(m))
        messages = merged
        # Empty turns are rejected too.
        for m in messages:
            if not (m["content"] or "").strip():
                m["content"] = "(tool call)" if m["role"] == "assistant" else "(no text)"

        if stream:
            try:
                sse_iter, _uheaders = upstream_chat_stream(messages, turnstile_token=turnstile)
            except UpstreamError as e:
                return send_mapped_error(self, e)
            except Exception as e:
                return openai_error(self, 502, f"could not reach upstream: {e}", "upstream_error", retry_after=3)
            self._serve_stream(sse_iter, req_model, dropped_images)
        else:
            self._serve_buffered(messages, req_model, dropped_images, catalog, turnstile)

    # -- responders --
    def _serve_stream(self, sse_iter, req_model: str, dropped_images: bool):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self._cors()
        if dropped_images:
            self.send_header("X-Warning", "image_url parts dropped: upstream text-only")
        self.end_headers()
        created = int(time.time())
        chat_id = f"chatcmpl-ox-{created}"
        # initial role chunk (OpenAI convention)
        self.wfile.write(f"data: {json.dumps({'id': chat_id, 'object': 'chat.completion.chunk', 'created': created, 'model': req_model, 'choices': [{'index': 0, 'delta': {'role': 'assistant'}, 'finish_reason': None}]})}\n\n".encode())
        deadline = time.time() + STREAM_MAX_SECONDS

        def _err_chunk():
            self.wfile.write(f"data: {json.dumps({'id': chat_id, 'object': 'chat.completion.chunk', 'created': created, 'model': req_model, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'error'}]})}\n\n".encode())
        try:
            for line in sse_iter:
                if time.time() > deadline:  # stalled upstream: fail explicitly
                    _err_chunk()
                    break
                if line.startswith(":"):  # upstream keep-alive -> comment ping
                    self.wfile.write(b": ping\n\n")
                    continue
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                if payload.startswith("[DONE"):
                    break
                try:
                    obj = json.loads(payload)
                except Exception:
                    continue
                # error event inside stream?
                if isinstance(obj, dict) and ("status" in obj or "error" in obj) and "choices" not in obj:
                    err = json.dumps(obj)
                    self.wfile.write(f"data: {json.dumps({'id': chat_id, 'object': 'chat.completion.chunk', 'created': created, 'model': req_model, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'error'}]})}\n\n".encode())
                    self.wfile.write(f"data: {json.dumps({'error': err})}\n\n".encode())
                    continue
                # normal OpenRouter/OpenAI chunk -> re-emit with requested model name
                try:
                    choices = obj.get("choices", [])
                    delta = (choices[0].get("delta") or {}) if choices else {}
                    content = delta.get("content")
                    reasoning = delta.get("reasoning")  # some models stream this; surface as content-less
                    finish = (choices[0].get("finish_reason") if choices else None)
                    if content is None and reasoning and not finish:
                        continue  # skip pure-reasoning deltas (no OpenAI field for it here)
                    out = {"id": obj.get("id", chat_id), "object": "chat.completion.chunk",
                           "created": obj.get("created", created), "model": req_model,
                           "choices": [{"index": 0,
                                        "delta": ({letter: content} if False else {"content": content or ""}),
                                        "finish_reason": finish}]}
                    # keep role on first content chunk for strict clients
                    if content:
                        out["choices"][0]["delta"] = {"content": content}
                    else:
                        out["choices"][0]["delta"] = {}
                        if finish is None:
                            continue
                    self.wfile.write(f"data: {json.dumps(out)}\n\n".encode())
                except Exception:
                    continue
            self.wfile.write(b"data: [DONE]\n\n")
            try:
                self.wfile.flush()
            except Exception:
                pass
            # SSE is strictly one-shot: the Connection: keep-alive header above
            # flips close_connection off, so without this the handler would sit
            # waiting for a second request and every SSE client would hang on EOF.
            self.close_connection = True
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (TimeoutError, OSError) as e:
            # upstream stalled mid-stream (timeout / dead conn): send an explicit
            # error chunk + [DONE] so clients never hang on a silent stream.
            try:
                _err_chunk()
                self.wfile.write(b"data: [DONE]\n\n")
            except Exception:
                pass

    def _serve_buffered(self, messages: list[dict], req_model: str, dropped_images: bool,
                        catalog: list[dict], turnstile: str | None):
        created = int(time.time())
        chat_id = f"chatcmpl-ox-{created}"
        upstream_id = chat_id
        finish = "stop"
        text = ""
        usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        # The upstream occasionally streams reasoning tokens only, or nothing at
        # all. Retry a bounded number of times before surfacing an error so
        # callers never see an empty completion; the per-attempt budgets keep
        # the worst case under the ~180s ceiling typical OpenAI clients enforce
        # (REQUEST_DEADLINE guards the whole loop; on exhaustion a retryable
        # 503 with Retry-After is returned so oreo's own retry plan takes over).
        deadline = time.time() + REQUEST_DEADLINE
        for budget in (90, 40, 40):
            remaining = deadline - time.time()
            if remaining < 20:
                return openai_error(self, 503, "request budget exhausted before a full answer; retry shortly",
                                    "rate_limit_exceeded", retry_after=3)
            budget = int(min(budget, remaining - 10))
            full: list[str] = []
            thought: list[str] = []
            finish = "stop"
            try:
                sse_iter, _uheaders = upstream_chat_stream(messages, turnstile_token=turnstile, timeout=budget)
            except UpstreamError as e:
                return send_mapped_error(self, e)
            except Exception as e:
                return openai_error(self, 502, f"could not reach upstream: {e}", "upstream_error", retry_after=3)
            try:
                for line in sse_iter:
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload in ("[DONE]", "[DONE]"):
                        break
                    try:
                        obj = json.loads(payload)
                    except Exception:
                        continue
                    if "choices" not in obj:
                        continue
                    try:
                        upstream_id = obj.get("id", upstream_id)
                        ch = obj["choices"][0]
                        d = ch.get("delta", {}) or {}
                        if d.get("content"):
                            full.append(str(d["content"]))
                        for rk in ("reasoning", "reasoning_content"):
                            if d.get(rk):
                                thought.append(str(d[rk]))
                        if ch.get("finish_reason"):
                            finish = ch["finish_reason"]
                        if obj.get("usage"):
                            u = obj["usage"]
                            for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                                if isinstance(u.get(k), int):
                                    usage[k] = u[k]
                    except Exception:
                        continue
            except UpstreamError as e:
                return send_mapped_error(self, e)
            except (BrokenPipeError, ConnectionResetError):
                return
            joined = "".join(full)
            # GLM-style <think> blocks inline in content are not the answer.
            stripped = re.sub(r"<think>[\s\S]*?</think>", "", joined).strip()
            text = stripped or "".join(thought).strip()
            if text:
                break
            time.sleep(1.2)
        if not text:
            return openai_error(self, 502, "upstream returned an empty response after retries; try again shortly",
                                "upstream_error", retry_after=3)
        # Function-calling bridge: a protocol reply becomes real OpenAI tool_calls.
        message: dict[str, Any] = {"role": "assistant"}
        if catalog:
            bridged = parse_tool_reply(text, catalog)
            if "calls" in bridged:
                message["content"] = None
                message["tool_calls"] = [
                    {"id": "call_" + f"{int(time.time() * 1000):x}{i:02d}", "type": "function",
                     "function": {"name": c["name"], "arguments": json.dumps(c["args"], separators=(",", ":"))}}
                    for i, c in enumerate(bridged["calls"])
                ]
                finish = "tool_calls"
            else:
                message["content"] = bridged["content"]
        else:
            message["content"] = text
        body = {"id": upstream_id, "object": "chat.completion", "created": created,
                "model": req_model,
                "choices": [{"index": 0, "message": message,
                             "finish_reason": finish}],
                "usage": usage}
        if dropped_images:
            body["warning"] = "image_url parts were dropped (upstream text-only)"
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self._cors()
        self.end_headers()
        self.wfile.write(data)


def _boot_ensure(no_autostart):
    """Solver-FIRST boot in the background: the server listens immediately
    (important for hosts like Replit that probe the port), and the solver is
    ensured concurrently — /health flips to ok when it's ready."""
    try:
        import chat as _chat
        _chat.SOLVER_BASE = SOLVER_BASE
        _chat.SOLVER_TIMEOUT = SOLVER_TIMEOUT
        if no_autostart:
            _chat.SOLVER_AUTOSTART = False
        _chat.ensure_solver()
    except Exception as e:
        print(f"solver ensure failed ({e}); continuing without auto-solve")


def main():
    global SOLVER_BASE
    ap = argparse.ArgumentParser(description="OxAlpha OpenAI-compatible proxy")
    ap.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "80")))
    ap.add_argument("--solver", default=None,
                    help="Boterdrop-Solver URL (default: http://127.0.0.1:8001, or SOLVER_BASE)")
    ap.add_argument("--no-solver", action="store_true", help="disable Turnstile auto-solve")
    ap.add_argument("--no-autostart", action="store_true",
                    help="don't auto-launch a local solver, only use a running one")
    args = ap.parse_args()
    if args.no_solver:
        SOLVER_BASE = ""
    elif args.solver:
        SOLVER_BASE = args.solver.rstrip("/")
    socketserver.ThreadingTCPServer.allow_reuse_address = True

    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True          # stuck handlers never block shutdown
        allow_reuse_address = True
        request_queue_size = 64        # burst tolerance for small agent fleets

    with Server((args.host, args.port), Handler) as httpd:
        httpd.allow_reuse_address = True
        print(f"oxalpha-openai-proxy on http://{args.host}:{args.port}")
        print(f"upstream {UPSTREAM_BASE} model {UPSTREAM_MODEL}")
        print(f"demo chat: http://{args.host}:{args.port}/")
        if SOLVER_BASE:
            print(f"solver {SOLVER_BASE} action={SOLVER_ACTION} "
                  f"({solver_health(SOLVER_BASE).get('status')}) — ensuring in background")
            # Solver-FIRST boot without blocking the port: launch a local
            # checkout when none answers, so the demo chats with no manual
            # verification once ready (chat.py's prober, daemon thread).
            import threading
            threading.Thread(target=_boot_ensure, args=(args.no_autostart,),
                             daemon=True).start()
        else:
            print("solver disabled (set SOLVER_BASE=http://127.0.0.1:8001 for Turnstile auto-solve)")
        print("endpoints: GET / (demo) | GET /v1/models | POST /v1/chat/completions (stream supported)")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
