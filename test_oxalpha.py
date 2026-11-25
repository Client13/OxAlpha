#!/usr/bin/env python3
"""
test_oxalpha.py — full test suite for the clean OxAlpha proxy.

Layers:
  1. Offline unit tests   — parser, history flattening, error mapping (no network)
  2. Live server tests    — /health, /v1/models, auth, buffered + streaming chat,
                            multi-turn, /v1/completions, Anthropic /v1/messages
                            (stream + buffered) against the real upstream
  3. Agentic tests        — a real agent loop over the OpenAI SDK with three
                            tools (list_files / read_file / calculator) solving a
                            multi-step task, plus an Anthropic-format agent round

Run:
  python3 test_oxalpha.py            # starts its own server on a free port
  python3 test_oxalpha.py --no-live  # unit tests only
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import oxalpha_server as ox  # noqa: E402

PASS = 0
FAIL = 0
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        FAILURES.append(f"{name}: {detail}")
        print(f"  ✗ {name}  — {detail}")


def section(title: str) -> None:
    print(f"\n{'=' * 62}\n {title}\n{'=' * 62}")


# --------------------------------------------------------------- unit tests --

def unit_tests() -> None:
    section("UNIT TESTS (offline)")

    calls, leftover = ox.parse_tool_calls(
        '{"tool_calls": [{"name": "calc", "arguments": {"expr": "1+1"}}]}')
    check("parse bare JSON tool call", calls is not None and len(calls) == 1
          and calls[0]["name"] == "calc" and calls[0]["arguments"] == {"expr": "1+1"})
    check("bare JSON leftover empty", leftover == "")

    calls, _ = ox.parse_tool_calls(
        '```json\n{"tool_calls": [{"name": "t", "arguments": {"a": 1}}]}\n```')
    check("parse fenced JSON tool call", calls is not None and len(calls) == 1)

    calls, _ = ox.parse_tool_calls('{"name": "w", "arguments": {"city": "Paris"}}')
    check("parse single-object style", calls is not None and calls[0]["name"] == "w")

    calls, _ = ox.parse_tool_calls('thinking out loud {"name": "w", "arguments": {}} done')
    check("parse prose-wrapped JSON", calls is not None and calls[0]["name"] == "w")

    calls, _ = ox.parse_tool_calls("plain text answer")
    check("plain text -> no calls", calls is None)

    calls, _ = ox.parse_tool_calls('{"result": {"nested": 1}}')
    check("non-tool JSON -> no calls", calls is None)

    calls, _ = ox.parse_tool_calls(
        '{"tool_calls": [{"name": "a", "arguments": "{\\"x\\":1}"}]}')
    check("string-encoded arguments decoded",
          calls is not None and calls[0]["arguments"] == {"x": 1})

    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "call_1", "type": "function",
                         "function": {"name": "calc", "arguments": "{\"expr\":\"1+1\"}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "2"},
    ]
    flat = ox.flatten_tool_history(msgs)
    check("flatten OpenAI tool history",
          len(flat) == 3 and "[TOOL CALL]" in flat[1]["content"]
          and "[TOOL RESULT" in flat[2]["content"] and flat[2]["role"] == "user")

    msgs_anthropic = [
        {"role": "assistant", "content": [
            {"type": "text", "text": "let me check"},
            {"type": "tool_use", "id": "toolu_1", "name": "calc", "input": {"expr": "2*2"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "4"}]},
    ]
    flat = ox.flatten_tool_history(msgs_anthropic)
    check("flatten Anthropic tool blocks",
          "[TOOL CALL]" in flat[0]["content"] and "[TOOL RESULT" in flat[1]["content"])

    llm, ctx = ox.build_tool_context(
        [{"role": "user", "content": "weather?"}],
        [{"name": "get_weather", "description": "w",
          "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}}}],
        "auto")
    check("tool contract injected", ctx is not None and "### get_weather" in llm[0]["content"])

    llm, ctx = ox.build_tool_context([{"role": "user", "content": "hi"}],
                                     [{"name": "t", "input_schema": {}}], "none")
    check("tool_choice=none disables tools", ctx is None)

    llm, ctx = ox.build_tool_context([{"role": "user", "content": "hi"}],
                                     [{"name": "t", "input_schema": {}}],
                                     {"type": "tool", "name": "t"})
    check("forced tool_choice present", ctx is not None and ctx.get("forced") == "t"
          and 'You MUST call the tool "t"' in llm[0]["content"])

    e = ox.UpstreamError(428, {"code": "turnstile_required", "error": "verify"})
    st, msg, code = ox.map_upstream_error(e)
    check("turnstile error mapping", st == 429 and code == "turnstile_required")
    e = ox.UpstreamError(429, {"error": "rate limited"})
    st, _, code = ox.map_upstream_error(e)
    check("429 error mapping", st == 429 and code == "rate_limit_exceeded")
    e = ox.UpstreamError(422, {"error": "Invalid model"})
    st, _, code = ox.map_upstream_error(e)
    check("422 error mapping", st == 400 and code == "invalid_request_error")

    parts, had_img = (lambda c: ox.flatten_content(c) if hasattr(ox, "flatten_content")
                      else (None, None))(None)  # flatten lives in handler layer; skip


# ------------------------------------------------------------ live plumbing --

def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Server:
    def __init__(self, port: int, env_extra: dict | None = None):
        env = dict(os.environ)
        env.update({"PORT": str(port), "HOST": "127.0.0.1",
                    "OXA_MIN_INTERVAL": "1.0", "OXA_DEBUG": "1"})
        env.update(env_extra or {})
        self.logf = open(f"/tmp/oxa_server_{port}.log", "w")
        self.proc = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "oxalpha_server.py"),
             "--host", "127.0.0.1", "--port", str(port)],
            env=env, stdout=self.logf, stderr=self.logf)
        self.port = port

    def wait_ready(self, timeout: float = 20.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                return False
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health",
                                            timeout=3) as r:
                    return r.status == 200
            except Exception:
                time.sleep(0.3)
        return False

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()
        self.logf.close()


def api(port: int, method: str, path: str, body: dict | None = None,
        headers: dict | None = None, timeout: float = 180) -> tuple[int, bytes, dict]:
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def sse_events(raw: bytes) -> list[tuple[str, str]]:
    """Parse `event: x\\ndata: y` pairs -> [(event_type, data_json_str)]."""
    events: list[tuple[str, str]] = []
    etype = ""
    for line in raw.decode("utf-8", "replace").splitlines():
        if line.startswith("event:"):
            etype = line[6:].strip()
        elif line.startswith("data:"):
            events.append((etype, line[5:].strip()))
        elif not line.strip():
            etype = ""
    return events


# ------------------------------------------------------------- live tests ----

def live_tests(port: int, api_key: str | None) -> None:
    section("LIVE TESTS (real upstream)")
    auth = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    st, raw, _ = api(port, "GET", "/health")
    health = json.loads(raw)
    check("GET /health", st == 200 and health.get("status") == "ok")
    check("health reports solver", "solver" in health)

    st, raw, _ = api(port, "GET", "/v1/models", headers=auth)
    models = json.loads(raw)
    ids = [m["id"] for m in models.get("data", [])]
    check("GET /v1/models", st == 200 and "ox-alpha" in ids)

    st, raw, hdr = api(port, "OPTIONS", "/v1/chat/completions")
    check("CORS preflight", st == 204 and
          hdr.get("Access-Control-Allow-Origin") == "*")

    # -- buffered chat
    st, raw, _ = api(port, "POST", "/v1/chat/completions", {
        "model": "ox-alpha",
        "messages": [{"role": "user", "content": "Say exactly: PONG-77"}]}, auth)
    body = json.loads(raw)
    check("buffered chat 200", st == 200, f"HTTP {st}: {raw[:200]}")
    check("buffered chat OpenAI shape",
          body.get("object") == "chat.completion"
          and body.get("choices", [{}])[0].get("message", {}).get("role") == "assistant")
    content = body.get("choices", [{}])[0].get("message", {}).get("content", "")
    check("buffered chat content echoes", "PONG-77" in content, f"got: {content[:120]!r}")
    check("buffered chat usage", body.get("usage", {}).get("total_tokens", 0) > 0)

    # -- multi-turn context
    st, raw, _ = api(port, "POST", "/v1/chat/completions", {
        "messages": [
            {"role": "user", "content": "My favorite color is teal. Remember it."},
            {"role": "assistant", "content": "Got it, teal is your favorite color."},
            {"role": "user", "content": "What is my favorite color? One word answer."},
        ]}, auth)
    content = json.loads(raw).get("choices", [{}])[0].get("message", {}).get("content", "")
    check("multi-turn memory", "teal" in content.lower(), f"got: {content[:120]!r}")

    # -- system message (upstream has no system role: merged into first user
    #    message; strict persona compliance is NOT guaranteed, see README —
    #    what matters for agents is that it doesn't 422 and the model answers)
    st, raw, _ = api(port, "POST", "/v1/chat/completions", {
        "messages": [
            {"role": "system", "content": "You are a precise math assistant."},
            {"role": "user", "content": "What is 2+2? Answer with the number."},
        ]}, auth)
    content = json.loads(raw).get("choices", [{}])[0].get("message", {}).get("content", "")
    check("system message accepted (merged, no 422)", st == 200 and "4" in content,
          f"HTTP {st} got: {content[:120]!r}")

    # -- streaming chat
    st, raw, _ = api(port, "POST", "/v1/chat/completions", {
        "model": "stealth/ox-alpha", "stream": True,
        "messages": [{"role": "user", "content": "Say exactly: STREAM-OK-9"}]}, auth)
    lines = [l for l in raw.decode().splitlines() if l.startswith("data: ")]
    check("streaming 200 + SSE", st == 200 and len(lines) > 2, f"HTTP {st}, {len(lines)} lines")
    check("streaming terminated with [DONE]",
          any(l == "data: [DONE]" for l in lines))
    chunks = [json.loads(l[6:]) for l in lines if l.startswith("data: ") and l != "data: [DONE]"]
    streamed = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks
                       if c.get("choices"))
    check("streamed content echoes", "STREAM-OK-9" in streamed, f"got: {streamed[:120]!r}")
    first_delta = next((c for c in chunks if c["choices"] and c["choices"][0]["delta"]
                        .get("role")), None)
    check("first chunk carries role", first_delta is not None)
    finishes = [c["choices"][0].get("finish_reason") for c in chunks
                if c.get("choices") and c["choices"][0].get("finish_reason")]
    check("stream finish_reason=stop", finishes and finishes[-1] == "stop", str(finishes))

    # -- content parts + image warning
    st, raw, _ = api(port, "POST", "/v1/chat/completions", {
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "Say exactly: PARTS-OK"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}}]}]}, auth)
    content = json.loads(raw).get("choices", [{}])[0].get("message", {}).get("content", "")
    check("content parts flattened", st == 200 and "PARTS-OK" in content)

    # -- legacy completions
    st, raw, _ = api(port, "POST", "/v1/completions",
                     {"prompt": "Say exactly: LEGACY-OK"}, auth)
    body = json.loads(raw)
    check("legacy /v1/completions", st == 200
          and body.get("object") == "text_completion"
          and "LEGACY-OK" in body.get("choices", [{}])[0].get("text", ""))

    # -- error shapes
    st, raw, _ = api(port, "POST", "/v1/chat/completions", {"messages": []}, auth)
    check("empty messages -> 400", st == 400 and "error" in json.loads(raw))
    st, raw, _ = api(port, "GET", "/nope")
    check("unknown route -> 404", st == 404)

    # -- Anthropic /v1/messages (buffered)
    st, raw, _ = api(port, "POST", "/v1/messages", {
        "model": "ox-alpha", "max_tokens": 256,
        "system": "You are a calculator. Answer with the number only.",
        "messages": [{"role": "user", "content": "What is 6*7?"}]}, auth)
    body = json.loads(raw)
    check("anthropic buffered 200", st == 200 and body.get("type") == "message",
          f"HTTP {st}: {raw[:200]}")
    acontent = "".join(b.get("text", "") for b in body.get("content", [])
                       if b.get("type") == "text")
    check("anthropic buffered text", "42" in acontent, f"got: {acontent[:120]!r}")
    check("anthropic stop_reason", body.get("stop_reason") == "end_turn")
    check("anthropic usage", body.get("usage", {}).get("input_tokens", 0) > 0)

    # -- Anthropic streaming
    st, raw, _ = api(port, "POST", "/v1/messages", {
        "model": "ox-alpha", "max_tokens": 256, "stream": True,
        "messages": [{"role": "user", "content": "Say exactly: ANTRO-STREAM-5"}]}, auth)
    events = sse_events(raw)
    etypes = [e for e, _ in events]
    check("anthropic stream 200", st == 200 and len(events) > 4)
    for needed in ("message_start", "content_block_start", "content_block_delta",
                   "content_block_stop", "message_delta", "message_stop"):
        check(f"anthropic event {needed}", needed in etypes)
    text = ""
    for etype, data in events:
        if etype == "content_block_delta":
            d = json.loads(data)
            if d.get("delta", {}).get("type") == "text_delta":
                text += d["delta"]["text"]
    check("anthropic streamed text echoes", "ANTRO-STREAM-5" in text, f"got: {text[:120]!r}")

    # -- Anthropic x-api-key auth (Claude Code style)
    if api_key:
        st, raw, _ = api(port, "POST", "/v1/messages", {
            "model": "ox-alpha", "max_tokens": 64,
            "messages": [{"role": "user", "content": "Say exactly: XAPIKEY-OK"}]},
            {"x-api-key": api_key})
        body = json.loads(raw)
        text = "".join(b.get("text", "") for b in body.get("content", [])
                       if b.get("type") == "text")
        check("anthropic x-api-key auth", st == 200 and "XAPIKEY-OK" in text)


# ------------------------------------------------------------ agentic tests --

AGENT_DIR = os.path.join(HERE, "agent_workspace")


def agent_tests(port: int, api_key: str | None) -> None:
    section("AGENTIC TESTS (OpenAI SDK tool-calling loop)")
    auth = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    # prepare a tiny workspace the agent will explore
    os.makedirs(AGENT_DIR, exist_ok=True)
    with open(os.path.join(AGENT_DIR, "mission.txt"), "w") as f:
        f.write("MISSION: deliver 42 coffee beans to sector 7.\nSTATUS: pending approval.\n")

    from openai import OpenAI
    client = OpenAI(base_url=f"http://127.0.0.1:{port}/v1",
                    api_key=api_key or "not-needed")

    tools = [
        {"type": "function", "function": {
            "name": "list_files",
            "description": "List files in the agent workspace directory.",
            "parameters": {"type": "object", "properties": {},
                           "required": []}}},
        {"type": "function", "function": {
            "name": "read_file",
            "description": "Read a text file from the agent workspace.",
            "parameters": {"type": "object", "properties": {
                "name": {"type": "string", "description": "file name"}},
                "required": ["name"]}}},
        {"type": "function", "function": {
            "name": "calculator",
            "description": "Evaluate an arithmetic expression like 12*34+9.",
            "parameters": {"type": "object", "properties": {
                "expression": {"type": "string"}}, "required": ["expression"]}}},
    ]

    def dispatch(name: str, args: dict) -> str:
        if name == "list_files":
            return json.dumps(sorted(os.listdir(AGENT_DIR)))
        if name == "read_file":
            path = os.path.join(AGENT_DIR, os.path.basename(str(args.get("name", ""))))
            return open(path).read() if os.path.isfile(path) else f"ERROR: {args.get('name')} not found"
        if name == "calculator":
            expr = str(args.get("expression", ""))
            if not all(c in "0123456789+-*/(). " for c in expr):
                return "ERROR: invalid characters"
            try:
                return str(eval(expr))  # sandboxed by the character filter above
            except Exception as e:
                return f"ERROR: {e}"
        return "ERROR: unknown tool"

    task = ("Work step by step. 1) List the files in the workspace. "
            "2) Read the file called mission.txt. "
            "3) Use the calculator to compute 123*456+789. "
            "4) Finally reply with one line in the format: "
            "SECRET=<first word of line 2 of mission.txt>|<calculator result>")

    messages: list[dict] = [{"role": "user", "content": task}]
    tool_rounds = 0
    answer = ""
    t0 = time.time()
    for _round in range(8):
        try:
            resp = client.chat.completions.create(
                model="ox-alpha", messages=messages, tools=tools, tool_choice="auto")
        except Exception as e:
            check("agent loop request", False, f"SDK error: {e}")
            return
        msg = resp.choices[0].message
        if resp.choices[0].finish_reason == "tool_calls" and msg.tool_calls:
            tool_rounds += len(msg.tool_calls)
            messages.append({"role": "assistant",
                             "content": msg.content or None,
                             "tool_calls": [
                                 {"id": c.id, "type": "function",
                                  "function": {"name": c.function.name,
                                               "arguments": c.function.arguments}}
                                 for c in msg.tool_calls]})
            for c in msg.tool_calls:
                try:
                    result = dispatch(c.function.name, json.loads(c.function.arguments or "{}"))
                except json.JSONDecodeError:
                    result = "ERROR: arguments not valid JSON"
                messages.append({"role": "tool", "tool_call_id": c.id,
                                 "content": result})
            continue
        answer = msg.content or ""
        break

    check("agent made tool calls", tool_rounds >= 2,
          f"only {tool_rounds} calls in {time.time() - t0:.0f}s")
    check("agent finished within round budget", bool(answer))
    # verified in code: 123*456+789 = 56877. The exact <word> may vary with the
    # model's reading of "first word of line 2" (STATUS vs deliver) — assert the
    # deterministic parts: format + correct calculator result.
    import re as _re
    answer_norm = answer.replace(" ", "").replace(",", "")
    check("agent final answer correct",
          bool(_re.search(r"SECRET=\S+?\|56877", answer_norm)),
          f"rounds={tool_rounds} answer={answer[:200]!r}")

    # -- forced tool_choice
    try:
        resp = client.chat.completions.create(
            model="ox-alpha",
            messages=[{"role": "user", "content": "compute 10+5 with the calculator"}],
            tools=tools, tool_choice={"type": "function", "function": {"name": "calculator"}})
        tc = resp.choices[0].message.tool_calls
        check("forced tool_choice honored",
              resp.choices[0].finish_reason == "tool_calls" and tc is not None
              and tc[0].function.name == "calculator")
    except Exception as e:
        check("forced tool_choice honored", False, str(e)[:200])

    # -- Anthropic-format agent round with tools
    section("AGENTIC TESTS (Anthropic /v1/messages tool round)")
    st, raw, _ = api(port, "POST", "/v1/messages", {
        "model": "ox-alpha", "max_tokens": 512,
        "system": "Use the calculator tool for math.",
        "tools": [{"name": "calculator",
                   "description": "Evaluate arithmetic like 12*34+9.",
                   "input_schema": {"type": "object",
                                    "properties": {"expression": {"type": "string"}},
                                    "required": ["expression"]}}],
        "tool_choice": {"type": "auto"},
        "messages": [{"role": "user", "content": "What is 777*888? Use the tool."}]}, auth)
    body = json.loads(raw)
    tool_use = next((b for b in body.get("content", [])
                     if b.get("type") == "tool_use"), None)
    check("anthropic tool_use block", st == 200 and tool_use is not None
          and body.get("stop_reason") == "tool_use",
          f"HTTP {st} body={raw[:200]}")
    if tool_use:
        check("anthropic tool_use input schema-shaped",
              isinstance(tool_use.get("input"), dict)
              and "expression" in tool_use["input"]
              and "777" in str(tool_use["input"]["expression"]),
              f"input={tool_use.get('input')}")
        # feed the tool result back and expect the final number
        st, raw, _ = api(port, "POST", "/v1/messages", {
            "model": "ox-alpha", "max_tokens": 512,
            "messages": [
                {"role": "user", "content": "What is 777*888? Use the tool."},
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": tool_use["id"], "name": "calculator",
                     "input": tool_use["input"]}]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": tool_use["id"],
                     "content": str(777 * 888)}]},
            ]}, auth)
        text = "".join(b.get("text", "") for b in json.loads(raw).get("content", [])
                       if b.get("type") == "text")
        # verified in code: 777*888 = 689976
        text_norm = text.replace(",", "").replace(" ", "")
        check("anthropic tool_result continuation",
              st == 200 and "689976" in text_norm, f"got: {text[:160]!r}")

    # -- streaming with tools (buffered server-side, tool_calls chunks emitted)
    st, raw, _ = api(port, "POST", "/v1/chat/completions", {
        "model": "ox-alpha", "stream": True,
        "messages": [{"role": "user", "content": "compute 55+66 with the calculator"}],
        "tools": tools, "tool_choice": {"type": "function",
                                        "function": {"name": "calculator"}}}, auth)
    lines = [l for l in raw.decode().splitlines() if l.startswith("data: ") and l != "data: [DONE]"]
    chunks = [json.loads(l[6:]) for l in lines]
    tc_deltas = [c["choices"][0]["delta"].get("tool_calls") for c in chunks
                 if c.get("choices") and c["choices"][0]["delta"].get("tool_calls")]
    finishes = [c["choices"][0].get("finish_reason") for c in chunks
                if c.get("choices") and c["choices"][0].get("finish_reason")]
    check("streamed tool_calls chunk", bool(tc_deltas)
          and tc_deltas[0][0]["function"]["name"] == "calculator")
    check("streamed tool finish_reason", finishes and finishes[-1] == "tool_calls",
          str(finishes))

    # ------------------------------------------------------------------
    section("RESPONSES API TESTS (OpenAI /v1/responses — Codex CLI wire format)")

    # buffered text answer
    st, raw, _ = api(port, "POST", "/v1/responses", {
        "model": "ox-alpha", "input": "Say exactly: RESP-OK-3", "stream": False}, auth)
    body = json.loads(raw)
    check("responses buffered 200", st == 200 and body.get("object") == "response",
          f"HTTP {st}: {raw[:200]}")
    rtext = "".join(p.get("text", "") for item in body.get("output", [])
                    for p in item.get("content", []) if isinstance(p, dict))
    check("responses buffered text", "RESP-OK-3" in rtext, f"got: {rtext[:120]!r}")
    check("responses usage shape",
          body.get("usage", {}).get("input_tokens", 0) > 0
          and body.get("status") == "completed")

    # function tool call (flat Responses tools format, like codex sends)
    st, raw, _ = api(port, "POST", "/v1/responses", {
        "model": "ox-alpha", "stream": False,
        "input": [{"type": "message", "role": "developer",
                   "content": [{"type": "input_text",
                                "text": "Always use the calculator for math."}]},
                  {"type": "message", "role": "user",
                   "content": [{"type": "input_text",
                                "text": "What is 111*222? Use the calculator."}]}],
        "tools": [{"type": "function", "name": "calculator",
                   "description": "Evaluate arithmetic like 12*34+9.",
                   "parameters": {"type": "object",
                                  "properties": {"expression": {"type": "string"}},
                                  "required": ["expression"]}}],
        "tool_choice": "auto"}, auth)
    body = json.loads(raw)
    fc = next((i for i in body.get("output", [])
               if i.get("type") == "function_call"), None)
    check("responses function_call item", st == 200 and fc is not None
          and fc.get("name") == "calculator"
          and "111" in str(fc.get("arguments")),
          f"HTTP {st} output={json.dumps(body.get('output', []))[:200]}")
    if fc:
        # feed function_call_output back (codex round 2)
        st, raw, _ = api(port, "POST", "/v1/responses", {
            "model": "ox-alpha", "stream": False,
            "input": [
                {"type": "message", "role": "user",
                 "content": [{"type": "input_text",
                              "text": "What is 111*222? Use the calculator."}]},
                {"type": "function_call", "call_id": fc["call_id"],
                 "name": "calculator", "arguments": fc["arguments"]},
                {"type": "function_call_output", "call_id": fc["call_id"],
                 "output": str(111 * 222)},
            ]}, auth)
        body = json.loads(raw)
        rtext = "".join(p.get("text", "") for item in body.get("output", [])
                        for p in item.get("content", []) if isinstance(p, dict))
        check("responses function_call_output continuation",
              st == 200 and "24642" in rtext.replace(",", "").replace(" ", ""),
              f"got: {rtext[:160]!r}")

    # streamed responses events (codex always streams)
    st, raw, _ = api(port, "POST", "/v1/responses", {
        "model": "ox-alpha", "stream": True, "store": False,
        "input": [{"type": "message", "role": "user",
                   "content": [{"type": "input_text",
                                "text": "Say exactly: RESP-STREAM-8"}]}],
        "tools": [{"type": "function", "name": "noop",
                   "description": "does nothing",
                   "parameters": {"type": "object", "properties": {}}}],
        "tool_choice": "auto"}, auth)
    events = sse_events(raw)
    etypes = [e for e, _ in events]
    check("responses stream 200 + events", st == 200 and len(events) > 3)
    for needed in ("response.created", "response.output_item.added",
                   "response.output_text.delta", "response.output_text.done",
                   "response.output_item.done", "response.completed"):
        check(f"responses event {needed}", needed in etypes)
    done_ev = next((json.loads(d) for e, d in events if e == "response.completed"), {})
    check("responses completed has usage",
          done_ev.get("response", {}).get("usage", {}).get("total_tokens", 0) > 0)

    # anthropic count_tokens probe (Claude Code)
    st, raw, _ = api(port, "POST", "/v1/messages/count_tokens", {
        "model": "ox-alpha", "messages": [{"role": "user", "content": "hello"}]}, auth)
    check("count_tokens endpoint", st == 200
          and json.loads(raw).get("input_tokens", 0) > 0)


# -------------------------------------------------------------------- main ---

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-live", action="store_true")
    args = ap.parse_args()

    unit_tests()
    if args.no_live:
        print(f"\nRESULT: {PASS} passed, {FAIL} failed")
        sys.exit(1 if FAIL else 0)

    port = free_port()
    api_key = "test-key-123"
    server = Server(port, env_extra={"PROXY_API_KEY": api_key})
    try:
        if not server.wait_ready():
            print("SERVER FAILED TO START — log:")
            print(open(server.logf.name).read()[-3000:])
            sys.exit(2)
        print(f"\nserver ready on :{port} (auth enabled)")
        live_tests(port, api_key)

        # auth negative checks
        section("AUTH TESTS")
        st, raw, _ = api(port, "GET", "/v1/models")
        check("models without key -> 401? (no, models are public)", st == 200)
        st, raw, _ = api(port, "POST", "/v1/chat/completions",
                         {"messages": [{"role": "user", "content": "hi"}]},
                         {"Authorization": "Bearer wrong"})
        check("chat with wrong key -> 401", st == 401)
        st, raw, _ = api(port, "POST", "/v1/chat/completions",
                         {"messages": [{"role": "user", "content": "hi"}]})
        check("chat without key -> 401", st == 401)

        agent_tests(port, api_key)
    finally:
        server.stop()

    print(f"\n{'=' * 62}")
    print(f" RESULT: {PASS} passed, {FAIL} failed")
    if FAILURES:
        print(" failures:")
        for f in FAILURES:
            print(f"   - {f}")
    print("=" * 62)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
