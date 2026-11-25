<<<<<<< HEAD
# OxAlpha — OpenAI-compatible API (solver + wrapper)

Turns the free OxAlpha web chat into a **stable OpenAI-compatible API**:

- `oxalpha-proxy/` — OpenAI-compatible wrapper (`/v1/chat/completions`, `/v1/models`, `/health`), pure-Python stdlib, Bearer-key auth, tool-calling bridge for agent frameworks, SSE streaming, bounded retries, OpenAI-style error envelopes.
- `boterdrop-solver/` — Camoufox-based Turnstile solver (auto-solves oxalpha.com challenges so the API never stalls on human verification).

**Status: deployed on Render and verified against the Oreo client contract (17/17 tests).**

## Deployed endpoints

| Path | Purpose |
| --- | --- |
| `GET /health` | liveness + solver status (public) |
| `GET /v1/models` | model list: `ox-alpha`, `stealth/ox-alpha`, `z-ai/glm-5.3-flash` |
| `POST /v1/chat/completions` | chat completions, `stream: true/false`, OpenAI `tools` supported |
| `GET /` | built-in demo chat app |

## Render deployment (this repo)

One Docker web service runs **both** components (solver on loopback `:8001`, proxy on `$PORT`). Blueprint: [`render.yaml`](render.yaml).

**Client config (oreo or any OpenAI SDK):**

```bash
OREO_BASE_URL=https://oxalpha-api.onrender.com/v1
OREO_API_KEY=sk-oxalpha-oreo-dummy-key   # dummy key — rotate in the Render dashboard
OREO_MODEL=ox-alpha
```

Quick check:

```bash
curl https://oxalpha-api.onrender.com/health
curl https://oxalpha-api.onrender.com/v1/chat/completions \
  -H "Authorization: Bearer sk-oxalpha-oreo-dummy-key" \
  -H "Content-Type: application/json" \
  -d '{"model":"ox-alpha","stream":false,"messages":[{"role":"user","content":"hi"}]}'
```

**Free plan note:** free instances sleep after 15 min without traffic; the first request after a sleep takes ~1–2 min (container start + browser boot). Switch `plan: free` → `starter` in `render.yaml` for always-on latency, or ping `/health` every 10 min to keep it warm.

## What oreo's provider sends (all supported)

`POST {base}/chat/completions` with `Authorization: Bearer <key>` and body `{model, messages, tools?, stream:false}` — system/developer prompts are folded into the opening user turn (upstream is text-only), content-parts arrays are flattened, assistant `tool_calls`/`tool` results are re-rendered, and tool schemas are bridged to real OpenAI `tool_calls` responses. Transient upstream failures return `429/500/502/503/504` + `Retry-After`, which oreo's retry plan re-drives; the request deadline is capped at 165 s so oreo's 180 s client timeout never fires first.

## Local run (no Docker)

```bash
pip install -r requirements.txt
python -m camoufox fetch
cd boterdrop-solver && python api_server.py          # :8001
cd oxalpha-proxy && PROXY_API_KEY=sk-oxalpha-oreo-dummy-key \
  SOLVER_BASE=http://127.0.0.1:8001 python server.py --host 0.0.0.0 --port 8000
```

## MCP setup

`mcp.example.json` is a ready-to-copy MCP client config:

- **Render MCP** (verified): remote server `https://mcp.render.com/mcp`, header `Authorization: Bearer <RENDER_API_KEY>`.
- **GitHub MCP**: official remote (`https://api.githubcopilot.com/mcp/`) or local `npx @modelcontextprotocol/server-github` with a PAT.

## Repository map

| Path | What |
| --- | --- |
| `oxalpha-proxy/server.py` | the API server (deploy this) |
| `oxalpha-proxy/turnstile_solver.py` | solver client (task API + health probe) |
| `oxalpha-proxy/chat.py` | standalone terminal REPL (og) |
| `boterdrop-solver/api_server.py` | Camoufox solver service (deploy this) |
| `main.py`, `start_services.sh` | local/Replit runners (og, untouched) |
| `deploy/` | Dockerfile + start script (Render) |
| `render.yaml` | Render blueprint (add the oreo webapp here later) |

Use responsibly: oxalpha.com is a free, rate-limited demo; this proxy is for personal/low-volume use.
=======
# 0xalpha — clean rebuild

One stdlib-only Python service that turns the free **oxalpha.com** chat upstream
(`z-ai/glm-5.3-flash`, OpenRouter-style SSE) into real agent-grade APIs.

**Zero required dependencies.** `python3 oxalpha_server.py` — that's it.

```
├── oxalpha_server.py     the whole service (OpenAI + Anthropic + Responses APIs)
├── turnstile_solver.py   optional Cloudflare Turnstile solver (patchright/playwright)
├── test_oxalpha.py       74-check test suite incl. live agentic tool-calling loops
└── README.md
```

## Why the old zip never worked (and what changed)

| Old failure | Now |
|---|---|
| `boterdrop-solver/api_server.py` imported `camoufox` at module level *before* its own auto-installer could run → instant `ImportError` crash | No camoufox. The builtin solver lazily imports **patchright** (falls back to playwright) only when a challenge actually appears |
| Replit-only `LD_PRELOAD=/nix/store/...`, interactive prompts, port juggling | None. Plain argparse + env vars, exits cleanly on a taken port |
| Three moving parts (solver sidecar + proxy + Next.js app needing 500 MB install and a missing `DATABASE_URL`) | **One process.** The Next.js UI is replaced by a built-in demo page at `/` |
| No tool-calling → codex/claude-code-style agents couldn't do anything | Full tool emulation (see below) |
| Upstream started rejecting `role:"system"` (HTTP 422 "Invalid messages") | System prompts are merged into the first user message automatically |

## Endpoints

| Method | Path | What |
|---|---|---|
| GET | `/` | demo chat page (streaming) |
| GET | `/health` | upstream + solver status |
| GET | `/v1/models` | `ox-alpha`, `stealth/ox-alpha`, `z-ai/glm-5.3-flash` |
| POST | `/v1/chat/completions` | **OpenAI chat API** — stream + buffered, `tools`/`tool_calls` supported |
| POST | `/v1/completions` | legacy text completions |
| POST | `/v1/responses` | **OpenAI Responses API** — what Codex CLI (≥0.172) speaks; `function_call` items + full SSE event stream |
| POST | `/v1/messages` | **Anthropic Messages API** — what Claude Code speaks; `tool_use`/`tool_result` blocks + SSE events |
| POST | `/v1/messages/count_tokens` | Anthropic token-count probe |

Models: any requested model name is echoed back; everything routes to the same
upstream model.

## Run

```bash
python3 oxalpha_server.py                 # http://127.0.0.1:8000
# options: --host 0.0.0.0 --port 8000
```

Env knobs:

| Var | Default | Meaning |
|---|---|---|
| `PORT` / `HOST` | `8000` / `127.0.0.1` | bind address |
| `PROXY_API_KEY` | *(unset)* | if set, require `Authorization: Bearer …` or `x-api-key` |
| `OXA_MIN_INTERVAL` | `2.0` | min seconds between upstream calls (demo is ~30 req/min — be polite) |
| `SOLVER_MODE` | `auto` | `auto` / `builtin` / `external` / `off` |
| `SOLVER_BASE` | *(unset)* | external Boterdrop-compatible solver URL |
| `SOLVER_TIMEOUT` | `150` | seconds to wait for a token |
| `UPSTREAM_BASE` / `UPSTREAM_MODEL` | oxalpha.com / `z-ai/glm-5.3-flash` | override for debugging |
| `OXA_DEBUG` | `0` | `1` = verbose logging |

## How agentic tools work on a text-only upstream

Codex / Claude Code / the OpenAI SDK need `tools` + structured tool calls;
the upstream web API understands only `{role, content}` text. The bridge:

1. Tool schemas are serialized into an instruction contract sent with the
   first user message (the upstream has no system role — it 422s them).
2. The model answers with a JSON `{"tool_calls":[{name, arguments}]}` object;
   the proxy parses it back into real **OpenAI `tool_calls`**,
   **Anthropic `tool_use`** blocks, or **Responses `function_call`** items.
3. `role:"tool"` / `tool_result` / `function_call_output` history items are
   flattened into `[TOOL CALL]` / `[TOOL RESULT]` text the model understands.

Streaming requests *without* tools are passed through token-by-token in real
time; requests *with* tools buffer one round (needed to parse the JSON) and
then emit proper protocol chunks/events.

### Proven end-to-end (in `test_oxalpha.py` + manual CLI runs)

- **OpenAI SDK agent loop**: 3 tools, multi-round tool calling, correct final answer
- **Codex CLI 0.172** (`wire_api = "responses"`): executed a real shell task
  (`printf 'CODEX-TEST-OK' > hello.txt`) through this proxy
- **Claude Code 2.1.332**: executed a real file-creation task through
  `/v1/messages` (incl. its `role:"system"`-in-messages quirk)

### Point Codex CLI at it

`~/.codex/config.toml` (or `CODEX_HOME/config.toml`):

```toml
model = "ox-alpha"
model_provider = "oxalpha"

[model_providers.oxalpha]
name = "oxalpha"
base_url = "http://127.0.0.1:8000/v1"
env_key = "OXALPHA_API_KEY"
wire_api = "responses"   # codex ≥0.172 no longer supports "chat"
```

Then: `OXALPHA_API_KEY=dummy codex "your task"`.

### Point Claude Code at it

```bash
ANTHROPIC_BASE_URL=http://127.0.0.1:8000 \
ANTHROPIC_AUTH_TOKEN=dummy \
claude -p "your task" --model ox-alpha --dangerously-skip-permissions
```

### OpenAI SDK / curl

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"ox-alpha","messages":[{"role":"user","content":"hi"}],"stream":true}'
```

```python
from openai import OpenAI
c = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="not-needed")
print(c.chat.completions.create(
    model="ox-alpha",
    messages=[{"role": "user", "content": "Hello"}],
).choices[0].message.content)
```

## Turnstile (Cloudflare challenge) handling

Challenges are rare and only appear under rate-limit/abuse pressure. When one
does arrive (HTTP 428 / `turnstile_required`), the proxy mints a token:

- **builtin** — headless Chromium via **patchright** (recommended,
  `pip install patchright && patchright install chromium`) or playwright.
  Same technique as the browser site: serve a minimal page at the protected
  origin with the widget embedded, wait for the token, click if needed.
- **external** — any Boterdrop-Solver-compatible sidecar
  (`SOLVER_BASE=http://127.0.0.1:8001`), protocol `/turnstile` → `/result`.
- **off** — never solve; challenges surface as HTTP 429.

Honest note: the builtin solver was **tested in this sandbox and Cloudflare
refused to render the widget for every local browser** (headless chromium,
webdriver-hidden, headed under Xvfb, patchright) — datacenter-IP reputation is
the usual cause, not the code. On residential IPs the patchright approach is
the same one used by the popular open-source solvers. Known projects if you
need alternatives:

- github.com/Theyka/Turnstile-Solver (patchright-based, most popular)
- github.com/ismoiloffS/EzSolver (nodriver + real Chrome)
- github.com/topics/cloudflare-turnstile (topic index)

## Proxy pool — scrape, check, rotate (testing aid)

The upstream caps messages **per IP per day**. When you exhaust your own IP
during testing, `proxy_pool.py` scrapes free public HTTP proxies, verifies
them, and feeds them to the server as a rotating pool (each upstream call
gets a fresh IP; rate-limited proxies cool down 10 min, dead ones are retired
after 3 consecutive failures; if the pool is empty/stale the server falls
back to a direct connection automatically).

```bash
# scrape up to 300, check them (2-stage: HTTPS CONNECT judge, then real
# oxalpha.com reachability for the top 20 fastest survivors)
python3 proxy_pool.py --scrape --check --limit 300 --concurrency 64 --out proxies.json

# refresh an existing pool later
python3 proxy_pool.py --check-existing --out proxies.json
python3 proxy_pool.py --stats --out proxies.json

# run the server with the pool enabled (hot-reloads the file on every call)
PROXY_POOL=1 PROXY_POOL_FILE=proxies.json python3 oxalpha_server.py
```

Sources are keyless public lists (TheSpeedX, monosans, proxyscrape,
proxy-list.download, roosterkid). Typical yield from the wild: 3–6% of
scraped proxies pass stage 1, a fraction of those reach oxalpha.com — the
checker prints honest numbers. `proxies_seed.json` in this repo is the seed
pool from the last verified run; the Render service uses it automatically.

Use only for your own low-volume testing — this is not a scale tool, and
free proxies are untrusted third parties: **never send secrets through them**
(the upstream API only ever receives chat text).

## Deploy on Render

One-click path — Blueprint from this repo, or via dashboard:

- **runtime** python, **build** `pip install --no-cache-dir -r requirements.txt`,
  **start** `python3 oxalpha_server.py --host 0.0.0.0 --port $PORT`
- **health check** `/health`
- env: `PYTHON_VERSION=3.12.6`, `PROXY_API_KEY` (Render-generated),
  `SOLVER_MODE=off` (datacenter IP + no chromium), `PROXY_POOL=1`,
  `PROXY_POOL_FILE=proxies_seed.json`, `OXA_MIN_INTERVAL=2.0`

`render.yaml` in the repo root encodes all of it. Free instances sleep after
~15 min idle; first request after a nap takes ~30–60 s to boot.

## Honest limitations

- **oxalpha.com is a free, rate-limited demo** (~30 req/min, IP daily caps).
  This proxy throttles by default and is for personal, low-volume use. For
  production, use official OpenAI-compatible resellers of the same model.
- **System prompts are best-effort.** The upstream has no system role and the
  model treats user-embedded instructions as advisory (it sometimes says so).
  Agentic tool contracts still work reliably because they are grounded by
  real tool results.
- Reverse-engineered upstream: the site can change its bundle, model id or
  anti-bot at any time. If `/chat` layout changes, `SessionManager` will fail
  loudly with a clear error.
- Tool calls are single-round JSON parsing; extremely long tool outputs are
  truncated to keep prompts sane? — no truncation is applied; keep outputs
  small in your own tools.

## Test

```bash
python3 test_oxalpha.py            # 74 checks: unit + live + agentic
python3 test_oxalpha.py --no-live  # offline unit tests only
```

Note: the live suite makes ~30 upstream calls and the upstream enforces a
**daily per-IP cap (150 messages/day)**. If you see clean `429: Message limit
reached` errors, the cap for today's IP is used up — the proxy surfaces it
correctly in both OpenAI and Anthropic error shapes; re-run tomorrow or from
another IP. (All 74 checks were recorded passing on a fresh quota, including
the Codex CLI and Claude Code end-to-end runs.)
>>>>>>> origin/main

---

## Parallel line: `oxalpha_server.py` (clean rebuild, also in this repo)

A second, independent implementation lives at the repo root — a **single-file,
stdlib-only** server (`oxalpha_server.py`) speaking **OpenAI + OpenAI Responses
+ Anthropic Messages** APIs, with its own patchright-based Turnstile solver
(`turnstile_solver.py`), an upstream **proxy pool** (`proxy_pool.py` +
`proxies_seed.json`) and a 74-check suite (`test_oxalpha.py`). Run it with:

```bash
python3 oxalpha_server.py                 # :8000
./start.sh                                # same, PORT/HOST env aware
```

Its Render blueprint strategy (SOLVER_MODE=off + PROXY_POOL=1) trades the
browser for rotating proxies; on this workspace the **UI-bridge Docker
deployment above is the proven path** (17/17 oreo-contract tests), so the
Render service builds `deploy/Dockerfile`. Both implementations coexist here;
pick per host:

| Host | Use |
| --- | --- |
| Render free (512MB) | Docker service → `deploy/Dockerfile` (UI bridge) |
| Fat VPS (2GB+) | either — `SOLVER_IMPL=boterdrop` in Docker, or `oxalpha_server.py` bare |
