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
