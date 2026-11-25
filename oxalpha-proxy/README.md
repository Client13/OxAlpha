# oxalpha-openai-proxy

OpenAI-compatible HTTP API in front of the free web chat at
`https://oxalpha.com/chat`. Standalone (`python3 server.py`, demo at `/`)
or hosted inside the workspace Express app, which auto-spawns it and serves
it under `/oxalpha` (demo `/oxalpha/`, `POST /oxalpha/v1/chat/completions`).
Lets any OpenAI SDK / agent framework talk to Ox Alpha
without an API key, by routing through the website's own `POST /api/chat` SSE endpoint.

Stdlib only — no `pip install`.

## How the website works (reverse-engineered 2026-09-05)

1. `GET /chat` sets `ox_alpha_session` + `XSRF-TOKEN` cookies and returns HTML with
   `<meta name="csrf-token" content="...">` and
   `window.__CHAT_MODELS__ = [{"id":"z-ai/glm-5.3-flash","slug":"ox-alpha",...}]`.
2. `POST /api/chat` with headers `X-CSRF-TOKEN`, `Cookie`, `Origin`, `Referer` and body
   `{"model":"z-ai/glm-5.3-flash","messages":[{"role":"user","content":"..."}]}`.
3. Reply is `text/event-stream` — raw OpenRouter chunks:
   `: OPENROUTER PROCESSING` keep-alives, then
   `data: {"choices":[{"delta":{"content":"..."}}],...}`, ending with usage chunk + `data: [DONE]`.
4. Abuse protection: HTTP 428 or JSON `{"code":"turnstile_required"}` (Cloudflare Turnstile,
   sitekey `0x4AAAAAABp_UYRVl7snkQ51`), plus `X-RateLimit-Limit: 30` headers and
   IP-based daily limits. The proxy surfaces these as HTTP 429 — it does **not**
   auto-solve captchas.

## Run

```bash
python3 server.py --port 8000
# demo chat with live Turnstile auto-solve (solver auto-boots if missing):
python3 server.py --port 8000   # then open http://127.0.0.1:8000/
# or
PORT=8000 python3 server.py
PROXY_API_KEY=secret python3 server.py  # require Authorization: Bearer secret
```

Open `http://127.0.0.1:8000/` for the built-in demo chat: streaming answers,
conversation history, New-chat reset, and a solver-status badge (from
`/health`). With the solver running, Turnstile challenges are solved
server-side and the demo chats uninterrupted; overloads get one automatic
retry. Flags: `--solver URL` / `--no-solver` / `--no-autostart`.

## Use

```bash
curl http://127.0.0.1:8000/v1/models

curl http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"ox-alpha","messages":[{"role":"user","content":"Say hi in 5 words"}],"stream":false}'

curl -N http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"stealth/ox-alpha","messages":[{"role":"user","content":"Hello"}],"stream":true}'
```

Python (OpenAI SDK):

```python
from openai import OpenAI
c = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="not-needed")
print(c.chat.completions.create(
    model="ox-alpha",
    messages=[{"role": "user", "content": "Explain recursion in one sentence"}],
).choices[0].message.content)
```

LangChain / agents: point `OPENAI_BASE_URL=http://127.0.0.1:8000/v1`,
`OPENAI_API_KEY=dummy`, `model=ox-alpha`.

## Turnstile auto-solve (solver + server + chat integrated)

When the upstream challenges you, the server can mint the token itself
through your own Boterdrop-Solver (same `/turnstile` + `/result` protocol as
that repo — see `turnstile_solver.py`), then retry with `X-Turnstile-Token`
exactly like the site's browser code does. Three terminals:

```bash
# 1. solver (needs a real browser + RAM, see ../boterdrop-solver)
cd 0xalpha/boterdrop-solver && python3 api_server.py        # port 8001

# 2. server, pointed at the solver (from the workspace root)
cd 0xalpha/oxalpha-proxy
SOLVER_BASE=http://127.0.0.1:8001 python3 server.py --port 8000

# 3. chat through the server (auto-solve happens inside it)
python3 chat.py --proxy http://127.0.0.1:8000/v1
```

Or skip the server — `chat.py` is solver-FIRST by default: on launch it
checks `http://127.0.0.1:8001` and auto-starts a local `boterdrop-solver`
checkout when missing (cloning it first if needed, installing its Python
deps, telling you exactly what to fix if the boot fails), then solves every
challenge on demand:

```bash
python3 chat.py
```

Knobs: `--solver URL` / `SOLVER_BASE` to point elsewhere,
`SOLVER_DIR` to point at the checkout, `--no-autostart` /
`SOLVER_AUTOSTART=0` to only use an already-running solver,
`--no-solver` to disable solving entirely, `SOLVER_START_TIMEOUT`
(seconds to wait for a cold start, default 300).

Notes: the token is minted for the exact widget oxalpha.com renders
(sitekey `0x4AAAAAABp_UYRVl7snkQ51`, action `chat`, page `/chat/`).
`GET /health` reports solver status. Without a solver (or with a bad
client token via `curl -H 'X-Turnstile-Token: ...'`), challenges still
surface as HTTP 429. Solving doesn't lift rate/daily limits — it only
answers the human-check.

## Terminal chat (`chat.py`)

Stdlib-only REPL that talks to the same backend directly and handles the
Turnstile/overload problem interactively (session refresh, backoff retries,
guided recovery — your message is never lost):

```bash
python3 chat.py                        # interactive chat
python3 chat.py --once "hello"         # one question, print answer, exit
python3 chat.py --proxy http://127.0.0.1:8000/v1   # same REPL via the proxy
```

Commands inside: `/new` fresh context, `/retry` resend last message,
`/system <text>` set a system prompt, `/save <file>` dump transcript,
`/quit` exit. When the upstream challenges you, it tells you to clear the
check at `https://oxalpha.com/chat` in a browser and press Enter to retry
(or paste an `X-Turnstile-Token` with `t <token>`).

## Limits & honesty

- Fragile by design: the site can change the bundle (`/chatapp/chat-app.min.js`),
  rotate the model id, or tighten Turnstile/rate limits at any time.
- Text-only: `image_url` content parts are dropped (web JSON path sends strings).
- For anything beyond tinkering, use the official OpenAI-compatible APIs instead:
  OpenRouter `stealth/ox-alpha`, Tokenra, AIMLAPI — same model, stable keys,
  no ToS gray area. Check oxalpha.com Terms before production use.
