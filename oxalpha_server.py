#!/usr/bin/env python3
"""
oxalpha_server.py — one clean service, three APIs, zero required dependencies.

A single stdlib-only Python process that turns the free oxalpha.com chat
upstream (z-ai/glm-5.3-flash, OpenRouter-style SSE) into:

  OpenAI-compatible      POST /v1/chat/completions   (stream + buffered)
                         GET  /v1/models
                         POST /v1/completions        (legacy, buffered)
  Anthropic-compatible   POST /v1/messages           (stream + buffered)
  Human                  GET  /                      (demo chat page)
  Ops                    GET  /health

Why agentic clients work on a text-only upstream
------------------------------------------------
Codex / Claude Code / OpenAI SDK agents speak `tools` + `tool_calls`, which the
upstream web API does not understand. This server bridges the gap by
instruction: tool schemas are serialized into a system contract, the model's
JSON reply is parsed back into real OpenAI `tool_calls` / Anthropic `tool_use`
blocks, and `role:"tool"` / `tool_result` messages are flattened into the
conversation. The result is a fully stateless, OpenAI-wire-compatible agent
backend.

Turnstile (Cloudflare challenge) handling
-----------------------------------------
Only when the upstream actually challenges (HTTP 428 / `turnstile_required`)
does this server mint a token, via:
  * SOLVER_MODE=builtin  — local headless-Chromium solver (Playwright), default
  * SOLVER_MODE=external — any Boterdrop-Solver-compatible sidecar (SOLVER_BASE)
  * SOLVER_MODE=auto     — external first (if configured), builtin fallback
  * SOLVER_MODE=off      — never solve; challenges surface as HTTP 429

Run:
  python3 oxalpha_server.py [--host 127.0.0.1] [--port 8000]

Env:
  PORT HOST UPSTREAM_BASE UPSTREAM_MODEL PROXY_API_KEY
  SOLVER_MODE SOLVER_BASE SOLVER_TIMEOUT OXA_MIN_INTERVAL OXA_DEBUG

Use responsibly: oxalpha.com is a free, rate-limited demo (~30 req/min).
This proxy throttles upstream calls (OXA_MIN_INTERVAL, default 2s) and is for
personal, low-volume use. For production, use official OpenAI-compatible
resellers of the same model instead.
"""

from __future__ import annotations

import argparse
import http.client
import itertools
import json
import logging
import os
import re
import secrets
import ssl
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Generator

import turnstile_solver as tsolver
import proxy_pool as ppool

# ------------------------------------------------------------------ config ---

UPSTREAM_BASE = os.environ.get("UPSTREAM_BASE", "https://oxalpha.com").rstrip("/")
UPSTREAM_HOST = urllib.parse.urlparse(UPSTREAM_BASE).hostname or "oxalpha.com"
UPSTREAM_MODEL = os.environ.get("UPSTREAM_MODEL", "z-ai/glm-5.3-flash")
MODEL_ALIASES = ["ox-alpha", "stealth/ox-alpha", UPSTREAM_MODEL]

BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
SESSION_TTL = 600.0          # seconds cookies+csrf stay fresh
MIN_INTERVAL = float(os.environ.get("OXA_MIN_INTERVAL", "2.0"))  # upstream throttle

TURNSTILE_SITEKEY = os.environ.get("OXA_TURNSTILE_SITEKEY", "0x4AAAAAABp_UYRVl7snkQ51")
TURNSTILE_URL = os.environ.get("OXA_TURNSTILE_URL", "https://oxalpha.com/chat/")
TURNSTILE_ACTION = os.environ.get("OXA_TURNSTILE_ACTION", "chat")

SOLVER_MODE = os.environ.get("SOLVER_MODE", "auto").strip().lower()
SOLVER_BASE = os.environ.get("SOLVER_BASE", "").rstrip("/")
SOLVER_TIMEOUT = float(os.environ.get("SOLVER_TIMEOUT", "150"))

# Optional free-proxy pool (see proxy_pool.py): PROXY_POOL=1 routes upstream
# calls through verified public proxies to spread per-IP daily caps.
PROXY_POOL_ENABLED = os.environ.get("PROXY_POOL", "0").lower() in ("1", "true", "yes")
PROXY_POOL_FILE = os.environ.get("PROXY_POOL_FILE", "proxies.json")

DEBUG = os.environ.get("OXA_DEBUG", "0").lower() in ("1", "true", "yes")

logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr,
)
log = logging.getLogger("oxalpha")

# ----------------------------------------------------------------- session ---


class UpstreamError(Exception):
    """An upstream failure with an HTTP-ish status and a JSON payload."""

    def __init__(self, status: int, payload: Any):
        super().__init__(str(payload)[:500])
        self.status = status
        self.payload = payload if isinstance(payload, dict) else {"error": str(payload)}

    @property
    def message(self) -> str:
        p = self.payload
        return str(p.get("error") or p.get("message") or json.dumps(p))[:2000]

    @property
    def is_turnstile(self) -> bool:
        return self.status == 428 or str(self.payload.get("code", "")) == "turnstile_required"


class SessionManager:
    """Caches GET /chat cookies + CSRF token; thread-safe; TTL refresh."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cookies: dict[str, str] = {}
        self._csrf = ""
        self._at = 0.0

    def _parse_cookies(self, headers: Any) -> dict[str, str]:
        out: dict[str, str] = {}
        for line in headers.get_all("Set-Cookie") or []:
            pair = line.split(";", 1)[0]
            if "=" in pair:
                k, v = pair.split("=", 1)
                out[k.strip()] = v.strip()
        return out

    def get(self, force: bool = False, proxy: str | None = None) -> dict:
        with self._lock:
            if not force and self._csrf and time.time() - self._at < SESSION_TTL:
                return {"cookies": dict(self._cookies), "csrf": self._csrf}
            try:
                with _open_url("GET", "/chat", None,
                               {"User-Agent": BROWSER_UA}, proxy, 30) as resp:
                    cookies = self._parse_cookies(resp.headers)
                    html = resp.read().decode("utf-8", "replace")
            except Exception as e:
                raise UpstreamError(502, {"error": f"GET {UPSTREAM_BASE}/chat failed: {e}"}) from e
            m = re.search(r'<meta name="csrf-token" content="([^"]+)"', html)
            if not m:
                raise UpstreamError(502, {"error": "could not extract csrf-token from /chat HTML "
                                                  "(upstream layout changed?)"})
            self._cookies, self._csrf, self._at = cookies, m.group(1), time.time()
            log.debug("session refreshed (csrf=%s…, cookies=%s)", self._csrf[:10], list(cookies))
            return {"cookies": dict(self._cookies), "csrf": self._csrf}


SESSION = SessionManager()

# ---------------------------------------------------------------- throttle ---

_throttle_lock = threading.Lock()
_throttle_last = 0.0


def _throttle() -> None:
    """Space out upstream POSTs to respect the ~30 req/min demo budget."""
    global _throttle_last
    if MIN_INTERVAL <= 0:
        return
    with _throttle_lock:
        wait = _throttle_last + MIN_INTERVAL - time.time()
        if wait > 0:
            time.sleep(wait)
        _throttle_last = time.time()


# ---------------------------------------------------------------- upstream ---

POOL = ppool.ProxyPool(PROXY_POOL_FILE, enabled=PROXY_POOL_ENABLED)
if PROXY_POOL_ENABLED:
    POOL.load()

_ssl_ctx = ssl.create_default_context()
PROXY_CONNECT_TIMEOUT = float(os.environ.get("PROXY_CONNECT_TIMEOUT", "8"))
UPSTREAM_READ_TIMEOUT = float(os.environ.get("UPSTREAM_READ_TIMEOUT", "180"))


def _open_url(method: str, url_path: str, body: bytes | None, headers: dict,
              proxy: str | None, timeout: float) -> http.client.HTTPResponse:
    """POST/GET against the upstream, optionally through an HTTP proxy.

    Proxied requests get a SHORT connect+tunnel+response-header budget
    (PROXY_CONNECT_TIMEOUT — dead free proxies fail fast) and switch to the
    full `timeout` only for streaming the body afterwards. Direct requests
    use `timeout` throughout."""
    if proxy:
        host, _, port = proxy.partition(":")
        conn = http.client.HTTPSConnection(host, int(port or 80),
                                           timeout=PROXY_CONNECT_TIMEOUT,
                                           context=_ssl_ctx)
        try:
            conn.set_tunnel(UPSTREAM_HOST, 443)
            conn.request(method, url_path, body=body, headers=headers)
            resp = conn.getresponse()
            if conn.sock:
                conn.sock.settimeout(timeout)
            return resp
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            raise
    conn = http.client.HTTPSConnection(UPSTREAM_HOST, 443, timeout=timeout,
                                       context=_ssl_ctx)
    conn.request(method, url_path, body=body, headers=headers)
    return conn.getresponse()


def _build_headers(sess: dict, token: str | None) -> dict[str, str]:
    h = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "X-CSRF-TOKEN": sess["csrf"],
        "Origin": UPSTREAM_BASE,
        "Referer": UPSTREAM_BASE + "/chat",
        "User-Agent": BROWSER_UA,
        "Cookie": "; ".join(f"{k}={v}" for k, v in sess["cookies"].items()),
    }
    if token:
        h["X-Turnstile-Token"] = token
    return h


def _post_chat(messages: list[dict], token: str | None,
               proxy: str | None = None) -> http.client.HTTPResponse:
    body = json.dumps({"model": UPSTREAM_MODEL, "messages": messages}).encode()
    sess = SESSION.get(proxy=proxy)
    try:
        return _open_url("POST", "/api/chat", body, _build_headers(sess, token),
                         proxy, UPSTREAM_READ_TIMEOUT)
    except Exception as e:
        raise UpstreamError(502, {"error": f"could not reach upstream "
                                           f"(via {proxy or 'direct'}): {e}"}) from e


def _read_error_body(resp: http.client.HTTPResponse) -> UpstreamError:
    try:
        raw = resp.read().decode("utf-8", "replace")
    except Exception:
        raw = ""
    finally:
        resp.close()
    if raw:
        try:
            payload = json.loads(raw)
            if isinstance(payload, dict):
                return UpstreamError(resp.status, payload)
        except Exception:
            pass
        return UpstreamError(resp.status, {"error": raw[:1000]})
    return UpstreamError(resp.status, {"error": f"upstream HTTP {resp.status}"})


def _mint_token() -> str:
    try:
        return tsolver.solve(TURNSTILE_URL, TURNSTILE_SITEKEY, TURNSTILE_ACTION,
                             timeout=SOLVER_TIMEOUT, mode=SOLVER_MODE,
                             external_base=SOLVER_BASE)
    except tsolver.SolverError as e:
        raise UpstreamError(429, {
            "code": "turnstile_required",
            "error": f"Turnstile auto-solve failed: {e}. Install playwright "
                     f"(pip install playwright && playwright install chromium) "
                     f"or point SOLVER_BASE at a Boterdrop-Solver instance.",
        }) from e


def upstream_stream(messages: list[dict], turnstile_token: str | None = None
                    ) -> Generator[tuple[str, Any], None, None]:
    """POST /api/chat and yield parsed upstream events.

    Proxy pool: when enabled, attempts go through pool proxies — connection
    failures hard-fail a proxy and retry the next (up to 3 tries); upstream
    429s soft-fail the proxy (long cooldown, its IP cap is likely used) and
    rotate to a fresh one.

    Yields: ("content", str) | ("reasoning", str) | ("finish", str|None)
            | ("usage", dict) | ("error", dict)
    Raises UpstreamError for pre-stream failures (with solve/retry logic).
    """
    _throttle()
    resp = None
    proxy = None
    tried: set[str] = set()
    last_err: UpstreamError | None = None
    t0 = time.time()
    for _attempt in range(3 if POOL.enabled else 1):
        proxy = POOL.pick(exclude=tried) if POOL.enabled else None
        if proxy:
            tried.add(proxy)
        t0 = time.time()
        try:
            resp = _post_chat(messages, turnstile_token, proxy)
            last_err = None
            break
        except UpstreamError as e:
            POOL.report_fail(proxy, hard=True)
            last_err = e
            log.warning("upstream attempt via %s failed: %s",
                        proxy or "direct", str(e)[:150])
    if resp is None and POOL.enabled and tried:
        log.info("all pool proxies failed (%d tried) — falling back to direct",
                 len(tried))
        t0 = time.time()
        try:
            resp = _post_chat(messages, turnstile_token, None)
            last_err = None
        except UpstreamError as e:
            last_err = e
    if resp is None:
        raise last_err or UpstreamError(502, {"error": "no upstream route available"})

    # CSRF rotation on 419
    if resp.status == 419:
        resp.close()
        SESSION.get(force=True, proxy=proxy)
        t0 = time.time()
        resp = _post_chat(messages, turnstile_token, proxy)

    def _challenge_like(r: http.client.HTTPResponse) -> bool:
        ctype = r.getheader("Content-Type") or ""
        return r.status != 200 or "application/json" in ctype

    # Non-200 (or 200 + JSON challenge) handling: read, classify, retry once.
    if _challenge_like(resp):
        err = _read_error_body(resp)
        if err.is_turnstile and not turnstile_token and SOLVER_MODE != "off":
            log.info("turnstile challenge received — minting token (mode=%s)", SOLVER_MODE)
            token = _mint_token()
            SESSION.get(force=True, proxy=proxy)  # challenge may be session-scoped
            _throttle()
            t0 = time.time()
            resp = _post_chat(messages, token, proxy)
            if _challenge_like(resp):
                err2 = _read_error_body(resp)
                if err2.is_turnstile:
                    raise UpstreamError(err2.status, {
                        **err2.payload,
                        "error": "auto-solve failed: solved token rejected by upstream "
                                 "(invalid/expired, or this IP is limited too). " + err2.message,
                    })
                raise err2
        elif err.status == 429 and not err.is_turnstile:
            POOL.report_fail(proxy, hard=False)  # proxy alive, but its IP cap is gone
            time.sleep(5)
            _throttle()
            proxy = POOL.pick(exclude=tried) if POOL.enabled else None
            if proxy:
                tried.add(proxy)
            t0 = time.time()
            resp = _post_chat(messages, turnstile_token, proxy)
            if _challenge_like(resp):
                raise _read_error_body(resp)
        else:
            raise err

    POOL.report_ok(proxy, latency_ms=int((time.time() - t0) * 1000))

    # Stream-parse the SSE body.
    buf = b""
    try:
        while True:
            chunk = resp.read(4096)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                text = line.decode("utf-8", "replace").rstrip("\r")
                ev = _parse_sse_line(text)
                if ev is not None:
                    yield ev
    except (http.client.IncompleteRead, ConnectionResetError) as e:
        log.warning("upstream stream interrupted: %s", e)
    finally:
        resp.close()


def _parse_sse_line(line: str) -> tuple[str, Any] | None:
    if not line.startswith("data:"):
        return None  # comments (`: OPENROUTER PROCESSING`) and event: lines ignored
    payload = line[5:].strip()
    if not payload:
        return None
    if payload == "[DONE]":
        return ("finish", "done")
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    if ("status" in obj or "error" in obj) and "choices" not in obj:
        return ("error", obj)
    choices = obj.get("choices") or []
    ch = choices[0] if choices and isinstance(choices[0], dict) else {}
    delta = ch.get("delta") or {}
    if isinstance(delta.get("content"), str) and delta["content"]:
        return ("content", delta["content"])
    if isinstance(delta.get("reasoning"), str) and delta["reasoning"]:
        return ("reasoning", delta["reasoning"])
    if ch.get("finish_reason"):
        return ("finish", ch["finish_reason"])
    if isinstance(obj.get("usage"), dict):
        return ("usage", obj["usage"])
    return None


# ---------------------------------------------------- tool-call emulation ----

TOOL_CONTRACT = """You have access to tools. To use one, respond with ONLY this JSON object (no markdown fences, no extra text):
{"tool_calls": [{"name": "<tool name>", "arguments": {<args object>}}]}
Rules:
- "arguments" must be a JSON object matching the tool's parameter schema.
- List multiple entries only when the calls are independent of each other.
- Tool results arrive back as messages containing [TOOL RESULT ...]; use them to continue the task.
- When no tool call is needed, reply in plain text.
Available tools:
"""


def _norm_schema(schema: Any) -> dict:
    return schema if isinstance(schema, dict) else {"type": "object", "properties": {}}


def build_tool_context(messages: list[dict], tools: list[dict] | None,
                       tool_choice: Any) -> tuple[list[dict], dict | None]:
    """Merge OpenAI/Anthropic tool definitions into an instruction contract.

    Returns (llm_messages, ctx). ctx is None when tools are absent/disabled.
    Messages containing tool_calls / role:"tool" are flattened to text the
    upstream understands.
    """
    if not tools:
        return flatten_tool_history(messages), None
    if tool_choice == "none":
        return flatten_tool_history(messages), None

    sections = [TOOL_CONTRACT]
    forced = None
    if isinstance(tool_choice, dict):
        forced = (tool_choice.get("function") or {}).get("name") or tool_choice.get("name")
    for t in tools:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        name = str(fn.get("name", "")).strip()
        if not name:
            continue
        schema = _norm_schema(fn.get("parameters") or fn.get("input_schema"))
        desc = str(fn.get("description") or "").strip()
        sections.append(f"### {name}\n{desc}\nparameters (JSON schema): "
                        f"{json.dumps(schema, ensure_ascii=False)}")

    if forced:
        sections.append(f'You MUST call the tool "{forced}" now (respond with its JSON '
                        f'tool_calls object).')
    contract = "\n".join(sections)

    # The upstream rejects role:"system", so attach the contract to the first
    # user message (create one if the history starts with an assistant turn).
    out = flatten_tool_history(messages)
    target = next((m for m in out if m["role"] == "user"), None)
    if target is not None:
        target["content"] = target["content"] + "\n\n" + contract
    else:
        out.insert(0, {"role": "user", "content": contract})
    return out, {"forced": forced}


def flatten_tool_history(messages: list[dict]) -> list[dict]:
    """OpenAI/Anthropic-ish history -> plain {role, content} text messages.

    The upstream API rejects role:"system" (HTTP 422 "Invalid messages"), so
    system instructions are merged into the first user message.
    """
    out: list[dict] = []
    system_texts: list[str] = []
    for m in messages:
        role = str(m.get("role", "user"))
        content = m.get("content")
        parts: list[str] = []
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, dict):
                    btype = block.get("type")
                    if btype == "text":
                        parts.append(str(block.get("text", "")))
                    elif btype == "tool_use":
                        parts.append("[TOOL CALL] " + json.dumps(
                            {"name": block.get("name"), "arguments": block.get("input") or {}},
                            ensure_ascii=False))
                    elif btype == "tool_result":
                        inner = block.get("content")
                        text = inner if isinstance(inner, str) else json.dumps(
                            inner, ensure_ascii=False) if inner is not None else ""
                        tag = f" (call {block.get('tool_use_id')})" if block.get("tool_use_id") else ""
                        if block.get("is_error"):
                            text = f"ERROR: {text}"
                        parts.append(f"[TOOL RESULT{tag}] {text}")
                    elif "text" in block:
                        parts.append(str(block.get("text")))
        elif content is not None:
            parts.append(str(content))
        # assistant tool_calls (OpenAI history shape)
        calls = m.get("tool_calls")
        if isinstance(calls, list):
            for c in calls:
                fn = (c.get("function") or {}) if isinstance(c, dict) else {}
                args = fn.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {"_raw": args}
                parts.append("[TOOL CALL] " + json.dumps(
                    {"name": fn.get("name"), "arguments": args or {}}, ensure_ascii=False))
        text = "\n".join(p for p in parts if p).strip()
        if role == "developer":
            role = "system"
        if role == "system":
            if text:
                system_texts.append(text)
            continue
        if role == "tool":
            # OpenAI-style tool output: mark it the same way Anthropic
            # tool_result blocks are marked, so the model sees one convention.
            tag = f" (call {m.get('tool_call_id')})" if m.get("tool_call_id") else ""
            text = f"[TOOL RESULT{tag}] {text or '(empty result)'}"
            role = "user"
        elif role not in ("user", "assistant"):
            role = "user"
        if not text:
            text = "(no content)"
        out.append({"role": role, "content": text})

    # merge system instructions into the first user message
    if system_texts and out:
        block = "[System instructions]\n" + "\n\n".join(system_texts) + "\n[/System instructions]"
        target = next((m for m in out if m["role"] == "user"), None)
        if target is not None:
            target["content"] = block + "\n\n" + target["content"]
        else:
            out.insert(0, {"role": "user", "content": block})
    elif system_texts:
        out.insert(0, {"role": "user",
                       "content": "[System instructions]\n" + "\n\n".join(system_texts)})
    return out


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def parse_tool_calls(text: str) -> tuple[list[dict] | None, str]:
    """Extract tool calls from the model reply.

    Returns (calls, leftover_text); calls is None when the reply is a normal
    text answer. Accepts bare JSON, fenced JSON, or prose ending in JSON.
    """
    if not text or "{" not in text:
        return None, text

    candidates: list[str] = [text.strip()]
    candidates += [m.strip() for m in _JSON_FENCE_RE.findall(text) if m.strip().startswith("{")]
    # balanced-brace scan so prose-prefixed JSON is found too
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch == "{":
            try:
                obj, _end = dec.raw_decode(text, i)
                candidates.append(json.dumps(obj, ensure_ascii=False))
            except json.JSONDecodeError:
                continue

    def _extract(obj: Any) -> list[dict] | None:
        if not isinstance(obj, dict):
            return None
        raw_calls = obj.get("tool_calls")
        if isinstance(raw_calls, list) and raw_calls:
            calls = []
            for c in raw_calls:
                if not isinstance(c, dict):
                    continue
                name = c.get("name") or (c.get("function") or {}).get("name")
                args = c.get("arguments") if "arguments" in c \
                    else (c.get("function") or {}).get("arguments")
                if c.get("input") is not None and "arguments" not in c:
                    args = c.get("input")
                if not name:
                    continue
                calls.append({"name": str(name), "arguments": _norm_args(args)})
            return calls or None
        name = obj.get("name")
        if name and ("arguments" in obj or "input" in obj or "parameters" in obj):
            args = obj.get("arguments", obj.get("input", obj.get("parameters", {})))
            return [{"name": str(name), "arguments": _norm_args(args)}]
        return None

    def _norm_args(args: Any) -> dict:
        if isinstance(args, dict):
            return args
        if isinstance(args, str) and args.strip():
            try:
                parsed = json.loads(args)
                return parsed if isinstance(parsed, dict) else {"_raw": args}
            except json.JSONDecodeError:
                return {"_raw": args}
        return {}

    # prefer candidates that actually decode to a tool-shaped object
    fallback: list[dict] | None = None
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except json.JSONDecodeError:
            continue
        calls = _extract(obj)
        if calls:
            leftover = _strip_json_block(text, cand)
            return calls, leftover
        if fallback is None and isinstance(obj, dict):
            fallback = None  # JSON but not tool-shaped: keep as text
    return None, text


def _strip_json_block(text: str, cand: str) -> str:
    """Remove the matched JSON (and its fence) from the reply text."""
    out = text
    if cand in out:
        out = out.replace(cand, "", 1)
    else:
        out = _JSON_FENCE_RE.sub(lambda m: "" if m.group(1).strip() == cand or
                                 m.group(1).strip() in cand else m.group(0), out, count=1)
    out = out.replace("```json", "").replace("```", "")
    return out.strip()


def _call_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(10)}"


# ----------------------------------------------------------- chat pipeline ---

class ChatResult:
    """Full completion of one upstream round (buffered view)."""

    def __init__(self) -> None:
        self.content = ""
        self.reasoning = ""
        self.finish: str | None = None
        self.usage: dict = {}


def collect_upstream(messages: list[dict], token: str | None = None) -> ChatResult:
    """Drain the upstream stream into a ChatResult (raises UpstreamError)."""
    res = ChatResult()
    for kind, payload in upstream_stream(messages, token):
        if kind == "content":
            res.content += payload
        elif kind == "reasoning":
            res.reasoning += payload
        elif kind == "finish" and payload != "done":
            res.finish = payload
        elif kind == "usage":
            res.usage = payload
        elif kind == "error":
            raise UpstreamError(502, payload)
    if not res.content and res.reasoning:
        res.content = res.reasoning  # some models answer purely via reasoning
        res.reasoning = ""
    return res


def run_chat(messages: list[dict], tools: list[dict] | None = None,
             tool_choice: Any = None) -> tuple[ChatResult, list[dict] | None]:
    """One upstream round with tool emulation.

    Returns (result, tool_calls) — tool_calls is a list of
    {"id","name","arguments"} or None when the model answered with plain text.
    """
    llm_messages, ctx = build_tool_context(messages, tools, tool_choice)
    res = collect_upstream(llm_messages)
    if ctx is None:
        return res, None
    calls, leftover = parse_tool_calls(res.content)
    if calls:
        for c in calls:
            c["id"] = _call_id("call")
        res.content = leftover
        if ctx.get("forced") and len(calls) == 1:
            calls[0]["name"] = ctx["forced"]  # keep forced choice honest
        return res, calls
    return res, None


def est_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


def usage_for(result: ChatResult, prompt_text: str) -> dict:
    u = result.usage if result.usage else {}
    return {
        "prompt_tokens": int(u.get("prompt_tokens") or est_tokens(prompt_text)),
        "completion_tokens": int(u.get("completion_tokens") or est_tokens(result.content)),
        "total_tokens": int(u.get("total_tokens") or
                            (est_tokens(prompt_text) + est_tokens(result.content))),
    }


# ------------------------------------------------------------- error maps ---

def openai_error_payload(status: int, message: str, code: str) -> dict:
    return {"error": {"message": message, "type": code, "code": code}}


def anthropic_error_payload(status: int, message: str, code: str) -> dict:
    atype = {"rate_limit_exceeded": "rate_limit_error",
             "invalid_request_error": "invalid_request_error",
             "turnstile_required": "rate_limit_error",
             "timeout": "timeout_error"}.get(code, "api_error")
    return {"type": "error", "error": {"type": atype, "message": message}}


def map_upstream_error(e: UpstreamError) -> tuple[int, str, str]:
    code = str(e.payload.get("code", ""))
    msg = e.message
    if code == "turnstile_required" or e.status == 428:
        return 429, ("Upstream oxalpha.com requested human verification "
                     "(Cloudflare Turnstile). " + msg), "turnstile_required"
    if e.status == 429 or "overload" in msg.lower() or "rate limit" in msg.lower():
        return 429, f"Upstream overloaded/rate-limited: {msg}", "rate_limit_exceeded"
    if e.status == 422 or "invalid model" in msg.lower():
        return 400, f"Upstream rejected request: {msg}", "invalid_request_error"
    if e.status in (401, 403):
        return 502, f"Upstream auth/config error: {msg}", "upstream_error"
    if e.status == 408:
        return 504, f"Upstream timed out: {msg}", "timeout"
    return 502, f"Upstream error ({e.status}): {msg}", "upstream_error"


# ------------------------------------------------------------- demo page ----

DEMO_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ox Alpha — clean proxy</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; font-family: system-ui, sans-serif; background:#0f1117; color:#e6e6e6;
         display:flex; flex-direction:column; height:100dvh; }
  header { padding:12px 16px; background:#161822; border-bottom:1px solid #2a2d3a;
           display:flex; gap:10px; align-items:center; }
  header b { flex:1; }
  #badge { font-size:12px; padding:3px 10px; border-radius:10px; background:#2a2d3a; }
  #badge.ok { background:#14532d; } #badge.bad { background:#7f1d1d; }
  #log { flex:1; overflow-y:auto; padding:16px; display:flex; flex-direction:column; gap:10px; }
  .msg { max-width:85%; padding:9px 13px; border-radius:12px; white-space:pre-wrap;
         word-wrap:break-word; line-height:1.5; }
  .user { align-self:flex-end; background:#1d4ed8; }
  .bot { align-self:flex-start; background:#1c1f2b; border:1px solid #2a2d3a; }
  .err { align-self:center; background:#7f1d1d; font-size:13px; max-width:95%; }
  form { display:flex; gap:8px; padding:12px; border-top:1px solid #2a2d3a; background:#161822; }
  input { flex:1; padding:11px; border-radius:9px; border:1px solid #333748;
          background:#0f1117; color:#eee; font-size:15px; }
  button { padding:11px 18px; border-radius:9px; border:0; background:#1d4ed8;
           color:#fff; cursor:pointer; font-size:15px; }
  button:disabled { opacity:.5; }
</style>
</head>
<body>
<header><b>Ox Alpha (clean proxy)</b><span id="badge">…</span>
<button id="new" type="button">New chat</button></header>
<div id="log"></div>
<form id="f"><input id="in" autocomplete="off" placeholder="Type a message…">
<button id="send" type="submit">Send</button></form>
<script>
const log=document.getElementById('log'),badge=document.getElementById('badge');
let history=[];
fetch('health').then(r=>r.json()).then(h=>{
  const s=(h.solver&&h.solver.status)||'unknown';
  badge.textContent='solver: '+s+' | model: '+(h.model||'?');
  badge.className=s==='ok'||s==='disabled'?'ok':'bad';
}).catch(()=>{badge.textContent='solver: ?';});
function add(cls,text){const d=document.createElement('div');d.className='msg '+cls;
  d.textContent=text;log.appendChild(d);log.scrollTop=log.scrollHeight;return d;}
document.getElementById('new').onclick=()=>{history=[];log.innerHTML='';};
document.getElementById('f').onsubmit=async e=>{
  e.preventDefault();
  const inp=document.getElementById('in'),btn=document.getElementById('send');
  const text=inp.value.trim();if(!text)return;
  inp.value='';btn.disabled=true;
  add('user',text);history.push({role:'user',content:text});
  const bubble=add('bot','…');bubble.textContent='';
  try{
    const r=await fetch('v1/chat/completions',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({model:'ox-alpha',messages:history,stream:true})});
    if(!r.ok){let msg='HTTP '+r.status;
      try{msg=(await r.json()).error.message||msg;}catch(_){}
      bubble.remove();add('err',msg);history.pop();btn.disabled=false;return;}
    const reader=r.body.getReader(),dec=new TextDecoder();
    let buf='',out='';
    for(;;){const{done,value}=await reader.read();if(done)break;
      buf+=dec.decode(value,{stream:true});
      const parts=buf.split('\n');buf=parts.pop();
      for(const line of parts){
        const t=line.trim();if(!t.startsWith('data:'))continue;
        const data=t.slice(5).trim();if(data==='[DONE]')continue;
        try{const c=JSON.parse(data).choices[0].delta.content;
          if(c){out+=c;bubble.textContent=out;log.scrollTop=log.scrollHeight;}}
        catch(_){}}}
    history.push({role:'assistant',content:out});
  }catch(err){bubble.remove();add('err',String(err));history.pop();}
  btn.disabled=false;
};
</script>
</body>
</html>
"""

# ---------------------------------------------------------------- handler ----

MAX_BODY = 10 * 1024 * 1024
CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "*, Authorization, x-api-key, Content-Type, X-Turnstile-Token",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "oxalpha-proxy/2.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # route through logging
        log.info("%s %s", self.address_string(), fmt % args)

    # -- plumbing --
    def _cors_headers(self):
        for k, v in CORS.items():
            self.send_header(k, v)

    def _send_json(self, status: int, payload: dict, extra: dict | None = None):
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self._cors_headers()
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _authed(self) -> bool:
        want = os.environ.get("PROXY_API_KEY", "")
        if not want:
            return True
        bearer = self.headers.get("Authorization", "")
        apikey = self.headers.get("x-api-key", "")
        return secrets.compare_digest(bearer, f"Bearer {want}") or \
            secrets.compare_digest(apikey, want)

    def _read_json(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if length > MAX_BODY:
            return None
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return None

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -- routing --
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            data = DEMO_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self._cors_headers()
            self.end_headers()
            self.wfile.write(data)
        elif path == "/health":
            solver = tsolver.health(SOLVER_MODE, SOLVER_BASE)
            self._send_json(200, {"status": "ok", "upstream": UPSTREAM_BASE,
                                  "model": UPSTREAM_MODEL, "solver": solver,
                                  "proxy_pool": POOL.stats(),
                                  "throttle_min_interval": MIN_INTERVAL,
                                  "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        elif path in ("/v1/models", "/models"):
            now = int(time.time())
            self._send_json(200, {"object": "list", "data": [
                {"id": mid, "object": "model", "created": now, "owned_by": "oxalpha",
                 "upstream": UPSTREAM_MODEL} for mid in MODEL_ALIASES]})
        else:
            self._send_json(404, openai_error_payload(
                404, f"unknown route {path}", "invalid_request_error"))

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path in ("/v1/chat/completions", "/chat/completions"):
                self._handle_chat_completions()
            elif path in ("/v1/completions", "/completions"):
                self._handle_legacy_completions()
            elif path == "/v1/messages":
                self._handle_anthropic_messages()
            elif path == "/v1/messages/count_tokens":
                self._handle_count_tokens()
            elif path in ("/v1/responses", "/responses"):
                self._handle_responses()
            else:
                self._send_json(404, openai_error_payload(
                    404, f"unknown route {path}", "invalid_request_error"))
        except (BrokenPipeError, ConnectionResetError):
            log.info("client disconnected during response")
        except Exception as e:  # never leak a stack trace as HTML 500
            log.exception("unhandled error on %s", path)
            try:
                self._send_json(500, openai_error_payload(
                    500, f"internal error: {e}", "api_error"))
            except Exception:
                pass

    # -- OpenAI: /v1/chat/completions --
    def _handle_chat_completions(self):
        if not self._authed():
            return self._send_json(401, openai_error_payload(
                401, "invalid API key (set PROXY_API_KEY on the server)", "invalid_request_error"))
        body = self._read_json()
        if body is None:
            return self._send_json(400, openai_error_payload(
                400, "invalid or oversized JSON body", "invalid_request_error"))

        req_model = str(body.get("model", "ox-alpha"))
        stream = bool(body.get("stream", False))
        tools = body.get("tools") if isinstance(body.get("tools"), list) else None
        tool_choice = body.get("tool_choice", "auto")
        turnstile = self.headers.get("X-Turnstile-Token") or body.get("turnstile_token")

        raw = body.get("messages")
        if not isinstance(raw, list) or not raw:
            return self._send_json(400, openai_error_payload(
                400, "`messages` must be a non-empty array", "invalid_request_error"))
        messages = []
        for m in raw:
            if not isinstance(m, dict) or "content" not in m:
                return self._send_json(400, openai_error_payload(
                    400, "each message needs {role, content}", "invalid_request_error"))
            messages.append(m)

        prompt_text = " ".join(str(m.get("content", "")) for m in messages)
        if stream and not tools:
            try:
                return self._openai_stream_passthrough(req_model, messages)
            except UpstreamError as e:
                st, msg, code = map_upstream_error(e)
                return self._send_json(st, openai_error_payload(st, msg, code))
        try:
            result, calls = run_chat(messages, tools, tool_choice)
        except UpstreamError as e:
            st, msg, code = map_upstream_error(e)
            return self._send_json(st, openai_error_payload(st, msg, code))
        except Exception as e:
            log.exception("chat failed")
            return self._send_json(502, openai_error_payload(
                502, f"could not reach upstream: {e}", "upstream_error"))

        usage = usage_for(result, prompt_text)
        if stream:
            self._openai_stream(req_model, result, calls, usage)
        else:
            self._openai_buffered(req_model, result, calls, usage)

    def _openai_id(self):
        return f"chatcmpl-ox-{int(time.time())}-{secrets.token_hex(4)}"

    def _openai_buffered(self, req_model: str, result: ChatResult,
                         calls: list[dict] | None, usage: dict):
        cid = self._openai_id()
        created = int(time.time())
        if calls:
            message = {"role": "assistant",
                       "content": result.content or None,
                       "tool_calls": [
                           {"id": c["id"], "type": "function",
                            "function": {"name": c["name"],
                                         "arguments": json.dumps(c["arguments"],
                                                                 ensure_ascii=False)}}
                           for c in calls]}
            finish = "tool_calls"
        else:
            message = {"role": "assistant", "content": result.content}
            finish = result.finish or "stop"
        payload = {"id": cid, "object": "chat.completion", "created": created,
                   "model": req_model, "choices": [
                       {"index": 0, "message": message, "finish_reason": finish}],
                   "usage": usage}
        if result.reasoning:
            payload["choices"][0]["message"]["reasoning_content"] = result.reasoning
        self._send_json(200, payload)

    def _openai_stream(self, req_model: str, result: ChatResult,
                       calls: list[dict] | None, usage: dict):
        cid = self._openai_id()
        created = int(time.time())
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self._cors_headers()
        self.end_headers()

        def chunk(delta: dict, finish: str | None = None):
            return {"id": cid, "object": "chat.completion.chunk", "created": created,
                    "model": req_model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

        try:
            self._sse(chunk({"role": "assistant", "content": ""}))
            if result.content:
                self._sse(chunk({"content": result.content}))
            if calls:
                for i, c in enumerate(calls):
                    self._sse(chunk({"tool_calls": [{
                        "index": i, "id": c["id"], "type": "function",
                        "function": {"name": c["name"],
                                     "arguments": json.dumps(c["arguments"],
                                                             ensure_ascii=False)}}]}))
            self._sse(chunk({}, "tool_calls" if calls else (result.finish or "stop")))
            # final chunk carries usage (OpenAI stream_options convention)
            self._sse({"id": cid, "object": "chat.completion.chunk", "created": created,
                       "model": req_model,
                       "choices": [{"index": 0, "delta": {}, "finish_reason": None}],
                       "usage": usage})
            self._sse_raw("data: [DONE]\n\n")
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _sse(self, obj: dict):
        self._sse_raw("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n")

    def _sse_raw(self, text: str):
        self.wfile.write(text.encode())
        self.wfile.flush()

    # -- passthrough streaming (tool-less requests: lowest latency) --
    def _start_passthrough(self, messages: list[dict]):
        """Start the upstream stream, surfacing pre-stream errors (turnstile,
        rate limits) BEFORE any HTTP 200/SSE headers are sent, so callers can
        still answer with a proper JSON error."""
        gen = upstream_stream(messages)
        try:
            first = next(gen)
        except StopIteration:
            first = None
        return gen, first

    def _openai_stream_passthrough(self, req_model: str, messages: list[dict]):
        gen, first = self._start_passthrough(messages)
        cid = self._openai_id()
        created = int(time.time())
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self._cors_headers()
        self.end_headers()

        def chunk(delta: dict, finish: str | None = None):
            return {"id": cid, "object": "chat.completion.chunk", "created": created,
                    "model": req_model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

        saw_content = False
        reasoning: list[str] = []
        usage: dict = {}
        out_chars = 0
        finish_name = "stop"
        errored = False
        try:
            self._sse(chunk({"role": "assistant", "content": ""}))
            events = itertools.chain([first] if first is not None else [], gen)
            for kind, payload in events:
                if kind == "content":
                    saw_content = True
                    out_chars += len(payload)
                    self._sse(chunk({"content": payload}))
                elif kind == "reasoning":
                    reasoning.append(payload)
                elif kind == "usage":
                    usage = payload
                elif kind == "error":
                    self._sse(chunk({}, "error"))
                    self._sse({"error": payload})
                    errored = True
                    break
                elif kind == "finish":
                    if payload == "done":
                        break
                    if not saw_content and reasoning:
                        self._sse(chunk({"content": "".join(reasoning)}))
                        saw_content = True
                    finish_name = payload
            if not errored:
                self._sse(chunk({}, finish_name or "stop"))
                completion = usage.get("completion_tokens") if usage else est_tokens("x" * out_chars)
                self._sse({"id": cid, "object": "chat.completion.chunk", "created": created,
                           "model": req_model,
                           "choices": [{"index": 0, "delta": {}, "finish_reason": None}],
                           "usage": {"prompt_tokens": est_tokens(" ".join(
                               str(m.get("content", "")) for m in messages)),
                               "completion_tokens": int(completion or max(1, out_chars // 4)),
                               "total_tokens": int((usage.get("total_tokens") if usage else 0)
                                                   or est_tokens(" ".join(str(m.get("content", ""))
                                                                          for m in messages))
                                                   + max(1, out_chars // 4))}})
                self._sse_raw("data: [DONE]\n\n")
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _anthropic_stream_passthrough(self, req_model: str, messages: list[dict],
                                      prompt_text: str):
        gen, first = self._start_passthrough(messages)
        mid = f"msg_{secrets.token_hex(12)}"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self._cors_headers()
        self.end_headers()

        def event(etype: str, data: dict):
            self._sse_raw(f"event: {etype}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n")

        saw_content = False
        reasoning: list[str] = []
        out_chars = 0
        stop_reason = "end_turn"
        try:
            event("message_start", {"type": "message_start", "message": {
                "id": mid, "type": "message", "role": "assistant", "model": req_model,
                "content": [], "stop_reason": None,
                "usage": {"input_tokens": est_tokens(prompt_text), "output_tokens": 0}}})
            event("ping", {"type": "ping"})
            event("content_block_start", {"type": "content_block_start", "index": 0,
                  "content_block": {"type": "text", "text": ""}})
            events = itertools.chain([first] if first is not None else [], gen)
            for kind, payload in events:
                if kind == "content":
                    saw_content = True
                    out_chars += len(payload)
                    event("content_block_delta", {"type": "content_block_delta",
                          "index": 0, "delta": {"type": "text_delta", "text": payload}})
                elif kind == "reasoning":
                    reasoning.append(payload)
                elif kind == "error":
                    stop_reason = "error"
                    break
                elif kind == "finish":
                    if payload == "done":
                        break
                    if not saw_content and reasoning:
                        event("content_block_delta", {"type": "content_block_delta",
                              "index": 0, "delta": {"type": "text_delta",
                              "text": "".join(reasoning)}})
                        saw_content = True
                    if payload == "length":
                        stop_reason = "max_tokens"
            event("content_block_stop", {"type": "content_block_stop", "index": 0})
            event("message_delta", {"type": "message_delta",
                  "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                  "usage": {"output_tokens": max(1, out_chars // 4)}})
            event("message_stop", {"type": "message_stop"})
        except (BrokenPipeError, ConnectionResetError):
            pass

    # -- legacy: /v1/completions --
    def _handle_legacy_completions(self):
        if not self._authed():
            return self._send_json(401, openai_error_payload(
                401, "invalid API key", "invalid_request_error"))
        body = self._read_json()
        if body is None:
            return self._send_json(400, openai_error_payload(
                400, "invalid or oversized JSON body", "invalid_request_error"))
        prompt = body.get("prompt", "")
        if isinstance(prompt, list):
            prompt = "\n".join(str(p) for p in prompt)
        if not str(prompt).strip():
            return self._send_json(400, openai_error_payload(
                400, "`prompt` must be non-empty", "invalid_request_error"))
        req_model = str(body.get("model", "ox-alpha"))
        try:
            result, _ = run_chat([{"role": "user", "content": str(prompt)}])
        except UpstreamError as e:
            st, msg, code = map_upstream_error(e)
            return self._send_json(st, openai_error_payload(st, msg, code))
        cid = f"cmpl-ox-{int(time.time())}"
        self._send_json(200, {"id": cid, "object": "text_completion",
                              "created": int(time.time()), "model": req_model,
                              "choices": [{"index": 0, "text": result.content,
                                           "finish_reason": "stop"}],
                              "usage": usage_for(result, str(prompt))})

    # -- Anthropic: /v1/messages --
    def _handle_anthropic_messages(self):
        if not self._authed():
            return self._send_json(401, anthropic_error_payload(
                401, "invalid API key (x-api-key header or Bearer token)",
                "invalid_request_error"))
        body = self._read_json()
        if body is None:
            return self._send_json(400, anthropic_error_payload(
                400, "invalid or oversized JSON body", "invalid_request_error"))

        req_model = str(body.get("model", "ox-alpha"))
        stream = bool(body.get("stream", False))
        raw_msgs = body.get("messages")
        if not isinstance(raw_msgs, list) or not raw_msgs:
            return self._send_json(400, anthropic_error_payload(
                400, "`messages` must be a non-empty array", "invalid_request_error"))
        system = body.get("system")
        messages: list[dict] = []
        if system:
            sys_text = system if isinstance(system, str) else "\n".join(
                str(b.get("text", "")) for b in system if isinstance(b, dict))
            if sys_text.strip():
                messages.append({"role": "system", "content": sys_text})
        for m in raw_msgs:
            if not isinstance(m, dict) or \
                    str(m.get("role")) not in ("user", "assistant", "system"):
                log.warning("anthropic 400: bad message item: %s",
                            json.dumps(m, ensure_ascii=False, default=str)[:600])
                return self._send_json(400, anthropic_error_payload(
                    400, "each message needs role user|assistant|system and content",
                    "invalid_request_error"))
            # tolerate missing content; claude code sends role:"system" items
            # inside the messages array — flatten_tool_history merges them.
            if "content" not in m:
                m = {**m, "content": ""}
            messages.append({"role": m["role"], "content": m.get("content")})

        tools = None
        raw_tools = body.get("tools")
        if isinstance(raw_tools, list) and raw_tools:
            tools = [{"name": t.get("name"), "description": t.get("description", ""),
                      "input_schema": t.get("input_schema")} for t in raw_tools
                     if isinstance(t, dict) and t.get("name")]
        tool_choice = body.get("tool_choice", "auto")
        if isinstance(tool_choice, dict) and tool_choice.get("type") == "any":
            tool_choice = "auto"
        if isinstance(tool_choice, dict) and tool_choice.get("type") == "tool":
            tool_choice = {"type": "tool", "name": tool_choice.get("name")}

        prompt_text = json.dumps(body.get("messages", []), ensure_ascii=False)[:8000]
        if stream and not tools:
            try:
                return self._anthropic_stream_passthrough(req_model, messages, prompt_text)
            except UpstreamError as e:
                st, msg, code = map_upstream_error(e)
                return self._send_json(st, anthropic_error_payload(st, msg, code))
        try:
            result, calls = run_chat(messages, tools, tool_choice)
        except UpstreamError as e:
            st, msg, code = map_upstream_error(e)
            return self._send_json(st, anthropic_error_payload(st, msg, code))
        except Exception as e:
            log.exception("anthropic chat failed")
            return self._send_json(502, anthropic_error_payload(
                502, f"could not reach upstream: {e}", "api_error"))

        usage = usage_for(result, prompt_text)
        if stream:
            self._anthropic_stream(req_model, result, calls, usage)
        else:
            self._anthropic_buffered(req_model, result, calls, usage)

    def _anthropic_blocks(self, result: ChatResult,
                          calls: list[dict] | None) -> tuple[list[dict], str]:
        if calls:
            content = [{"type": "tool_use", "id": _call_id("toolu"), "name": c["name"],
                        "input": c["arguments"]} for c in calls]
            return content, "tool_use"
        return [{"type": "text", "text": result.content or ""}], "end_turn"

    def _anthropic_buffered(self, req_model: str, result: ChatResult,
                            calls: list[dict] | None, usage: dict):
        content, stop = self._anthropic_blocks(result, calls)
        self._send_json(200, {
            "id": f"msg_{secrets.token_hex(12)}", "type": "message",
            "role": "assistant", "model": req_model, "content": content,
            "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": usage["prompt_tokens"],
                      "output_tokens": usage["completion_tokens"]}})

    def _anthropic_stream(self, req_model: str, result: ChatResult,
                          calls: list[dict] | None, usage: dict):
        mid = f"msg_{secrets.token_hex(12)}"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self._cors_headers()
        self.end_headers()

        def event(etype: str, data: dict):
            self._sse_raw(f"event: {etype}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n")

        try:
            event("message_start", {"type": "message_start", "message": {
                "id": mid, "type": "message", "role": "assistant",
                "model": req_model, "content": [], "stop_reason": None,
                "usage": {"input_tokens": usage["prompt_tokens"],
                          "output_tokens": 0}}})
            event("ping", {"type": "ping"})
            if calls:
                for i, c in enumerate(calls):
                    event("content_block_start", {"type": "content_block_start",
                          "index": i, "content_block": {"type": "tool_use",
                          "id": _call_id("toolu"), "name": c["name"], "input": {}}})
                    event("content_block_delta", {"type": "content_block_delta",
                          "index": i, "delta": {"type": "input_json_delta",
                          "partial_json": json.dumps(c["arguments"], ensure_ascii=False)}})
                    event("content_block_stop", {"type": "content_block_stop", "index": i})
                stop = "tool_use"
            else:
                event("content_block_start", {"type": "content_block_start", "index": 0,
                      "content_block": {"type": "text", "text": ""}})
                if result.content:
                    event("content_block_delta", {"type": "content_block_delta",
                          "index": 0, "delta": {"type": "text_delta",
                          "text": result.content}})
                event("content_block_stop", {"type": "content_block_stop", "index": 0})
                stop = "end_turn"
            event("message_delta", {"type": "message_delta",
                  "delta": {"stop_reason": stop, "stop_sequence": None},
                  "usage": {"output_tokens": usage["completion_tokens"]}})
            event("message_stop", {"type": "message_stop"})
        except (BrokenPipeError, ConnectionResetError):
            pass


    def _handle_count_tokens(self):
        """Anthropic token counting probe (Claude Code calls this on boot)."""
        if not self._authed():
            return self._send_json(401, anthropic_error_payload(
                401, "invalid API key", "invalid_request_error"))
        body = self._read_json() or {}
        total = est_tokens(json.dumps(body.get("messages", []), ensure_ascii=False)) \
            + est_tokens(str(body.get("system", "")))
        self._send_json(200, {"input_tokens": total})

    # -- OpenAI Responses API: /v1/responses (what modern Codex CLI speaks) --
    def _responses_input_to_messages(self, body: dict) -> list[dict]:
        """Responses API `input` items -> internal {role, content} messages."""
        messages: list[dict] = []
        instructions = body.get("instructions")
        if isinstance(instructions, str) and instructions.strip():
            messages.append({"role": "system", "content": instructions})
        raw = body.get("input")
        if isinstance(raw, str):
            messages.append({"role": "user", "content": raw})
            return messages
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            itype = item.get("type", "message")
            if itype == "message":
                content = item.get("content")
                parts: list[str] = []
                if isinstance(content, str):
                    parts.append(content)
                elif isinstance(content, list):
                    for c in content:
                        if isinstance(c, str):
                            parts.append(c)
                        elif isinstance(c, dict) and c.get("type") in (
                                "input_text", "output_text", "text", "summary_text"):
                            parts.append(str(c.get("text", "")))
                text = "\n".join(p for p in parts if p).strip()
                if text:
                    messages.append({"role": str(item.get("role", "user")),
                                     "content": text})
            elif itype == "function_call":
                args = item.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {"_raw": args}
                messages.append({"role": "assistant", "content": "[TOOL CALL] " +
                                 json.dumps({"name": item.get("name"),
                                             "arguments": args or {}},
                                            ensure_ascii=False)})
            elif itype == "function_call_output":
                out = item.get("output")
                if isinstance(out, list):
                    out = "\n".join(str(c.get("text", "")) if isinstance(c, dict) else str(c)
                                    for c in out)
                messages.append({"role": "tool", "tool_call_id": item.get("call_id"),
                                 "content": str(out or "")})
            # reasoning / item_reference / others: ignored
        return messages

    def _handle_responses(self):
        if not self._authed():
            return self._send_json(401, openai_error_payload(
                401, "invalid API key", "invalid_request_error"))
        body = self._read_json()
        if body is None:
            return self._send_json(400, openai_error_payload(
                400, "invalid or oversized JSON body", "invalid_request_error"))

        req_model = str(body.get("model", "ox-alpha"))
        messages = self._responses_input_to_messages(body)
        if not messages:
            return self._send_json(400, openai_error_payload(
                400, "`input` must be a string or a non-empty item list",
                "invalid_request_error"))

        tools = []
        for t in body.get("tools") or []:
            if isinstance(t, dict) and t.get("type") == "function" and t.get("name"):
                tools.append({"name": t["name"],
                              "description": t.get("description", ""),
                              "input_schema": t.get("parameters")
                              or {"type": "object", "properties": {}}})
        tool_choice = body.get("tool_choice", "auto")
        if isinstance(tool_choice, dict):
            tool_choice = {"type": "tool",
                           "name": tool_choice.get("name")
                           or (tool_choice.get("function") or {}).get("name")}

        prompt_est = est_tokens(json.dumps(messages, ensure_ascii=False)[:8000])
        try:
            result, calls = run_chat(messages, tools or None, tool_choice)
        except UpstreamError as e:
            st, msg, code = map_upstream_error(e)
            return self._send_json(st, openai_error_payload(st, msg, code))
        except Exception as e:
            log.exception("responses request failed")
            return self._send_json(502, openai_error_payload(
                502, f"could not reach upstream: {e}", "upstream_error"))

        usage = usage_for(result, "x" * prompt_est)
        if calls:
            for c in calls:
                c["id"] = _call_id("call")
        if body.get("stream"):
            self._responses_stream(req_model, result, calls, usage)
        else:
            self._responses_buffered(req_model, result, calls, usage)

    def _responses_output_items(self, result: ChatResult,
                                calls: list[dict] | None) -> list[dict]:
        items: list[dict] = []
        if result.content:
            items.append({"type": "message", "id": _call_id("msg"),
                          "role": "assistant",
                          "content": [{"type": "output_text",
                                       "text": result.content, "annotations": []}],
                          "status": "completed"})
        for c in calls or []:
            items.append({"type": "function_call", "id": _call_id("fc"),
                          "call_id": c["id"], "name": c["name"],
                          "arguments": json.dumps(c["arguments"], ensure_ascii=False),
                          "status": "completed"})
        if not items:
            items.append({"type": "message", "id": _call_id("msg"),
                          "role": "assistant",
                          "content": [{"type": "output_text",
                                       "text": "(empty response)", "annotations": []}],
                          "status": "completed"})
        return items

    def _responses_base(self, rid: str, req_model: str, items: list[dict],
                        usage: dict) -> dict:
        return {"id": rid, "object": "response", "created_at": int(time.time()),
                "status": "completed", "model": req_model, "output": items,
                "usage": {"input_tokens": usage["prompt_tokens"],
                          "output_tokens": usage["completion_tokens"],
                          "total_tokens": usage["total_tokens"],
                          "input_tokens_details": {"cached_tokens": 0},
                          "output_tokens_details": {"reasoning_tokens": 0}},
                "error": None, "incomplete_details": None, "instructions": None,
                "metadata": {}, "temperature": None, "top_p": None,
                "tool_choice": "auto", "tools": [], "parallel_tool_calls": True,
                "previous_response_id": None, "reasoning": None,
                "store": False, "text": {"format": {"type": "text"}}}

    def _responses_buffered(self, req_model: str, result: ChatResult,
                            calls: list[dict] | None, usage: dict):
        items = self._responses_output_items(result, calls)
        self._send_json(200, self._responses_base(_call_id("resp"), req_model,
                                                  items, usage))

    def _responses_stream(self, req_model: str, result: ChatResult,
                          calls: list[dict] | None, usage: dict):
        rid = _call_id("resp")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self._cors_headers()
        self.end_headers()
        seq = [0]

        def event(etype: str, data: dict):
            data = {"type": etype, "sequence_number": seq[0], **data}
            seq[0] += 1
            self._sse_raw(f"event: {etype}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n")

        try:
            event("response.created", {"response": self._responses_base(
                rid, req_model, [], usage)})
            event("response.in_progress", {"response": self._responses_base(
                rid, req_model, [], usage)})
            out_index = 0
            items = self._responses_output_items(result, calls)
            for item in items:
                if item["type"] == "message":
                    event("response.output_item.added",
                          {"output_index": out_index,
                           "item": {**item, "content": []}})
                    event("response.content_part.added",
                          {"item_id": item["id"], "output_index": out_index,
                           "content_index": 0,
                           "part": {"type": "output_text", "text": "",
                                    "annotations": []}})
                    text = item["content"][0]["text"]
                    if text:
                        event("response.output_text.delta",
                              {"item_id": item["id"], "output_index": out_index,
                               "content_index": 0, "delta": text})
                    event("response.output_text.done",
                          {"item_id": item["id"], "output_index": out_index,
                           "content_index": 0, "text": text})
                    event("response.content_part.done",
                          {"item_id": item["id"], "output_index": out_index,
                           "content_index": 0,
                           "part": {"type": "output_text", "text": text,
                                    "annotations": []}})
                    event("response.output_item.done",
                          {"output_index": out_index, "item": item})
                else:  # function_call
                    event("response.output_item.added",
                          {"output_index": out_index,
                           "item": {**item, "arguments": ""}})
                    args = item["arguments"]
                    event("response.function_call_arguments.delta",
                          {"item_id": item["id"], "output_index": out_index,
                           "delta": args})
                    event("response.function_call_arguments.done",
                          {"item_id": item["id"], "output_index": out_index,
                           "arguments": args})
                    event("response.output_item.done",
                          {"output_index": out_index, "item": item})
                out_index += 1
            event("response.completed", {"response": self._responses_base(
                rid, req_model, items, usage)})
        except (BrokenPipeError, ConnectionResetError):
            pass


# -------------------------------------------------------------------- main ---

def main() -> None:
    ap = argparse.ArgumentParser(description="OxAlpha clean proxy — OpenAI + Anthropic compatible")
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    args = ap.parse_args()

    ThreadingHTTPServer.daemon_threads = True
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    solver = tsolver.health(SOLVER_MODE, SOLVER_BASE)
    log.info("oxalpha clean proxy v2.1 on http://%s:%s", args.host, args.port)
    log.info("upstream %s | model %s", UPSTREAM_BASE, UPSTREAM_MODEL)
    log.info("solver: mode=%s status=%s backend=%s",
             solver["mode"], solver["status"], solver.get("backend"))
    if PROXY_POOL_ENABLED:
        log.info("proxy pool: ENABLED %s", POOL.stats())
    log.info("endpoints: GET / | /health | /v1/models  POST /v1/chat/completions | "
             "/v1/messages | /v1/completions")
    if os.environ.get("PROXY_API_KEY"):
        log.info("auth: enabled (PROXY_API_KEY)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("bye")


if __name__ == "__main__":
    main()
