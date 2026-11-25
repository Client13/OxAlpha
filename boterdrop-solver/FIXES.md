# Fix notes — issue #1: “Connection closed while reading from the driver”

Fixed in `api_server.py` (this folder is a copy of upstream
`najibyahya/Boterdrop-Solver` + the patches below). `test_robustness.py`
covers the new logic with fakes (no browser needed): 16/16 passing.

## Triage of open issues

- **#1 (real bug, fixed here):** after several successful requests the browser
  disconnects (`BrowserContext.cookies: Connection closed…`,
  `Page.route: Connection closed…`) and the solver never recovers without a
  manual restart.
- **#2 (“Add CaptchaAI as a backend?”, not implemented):** reads like a
  third-party promo for a paid 2Captcha-compatible API. No code attached, no
  maintainer request for it. Recommend closing as out-of-scope; it would add
  an external paid dependency to a self-hosted solver.
- Open PR #3 (Docker support) is unrelated to the crash.

## Root causes found (code review, no browser needed)

1. **No recovery path.** Every solver did `page, context = await
   page_pool.get()` (blocks forever when the pool is drained by a dead
   browser), and any `Connection closed` exception just marked one task
   failed while the dead pages stayed in circulation. One browser crash
   therefore wedged the server until a manual restart — exactly the report.
2. **Dead pages poisoned the pool.** `finally` blocks returned pages based
   only on `context in self.contexts`, never checking the driver was alive,
   so subsequent requests got dead pages and failed in cascade.
3. **Context leak in Turnstile proxy rotation.** On round 2 the old context
   was closed but never removed from `self.contexts`, and the new context
   was never registered — leaked Firefox contexts accumulate RAM until the
   driver dies.
4. **Unbounded reCAPTCHA concurrency.** `_solve_recaptcha` boots a whole new
   Chromium + Playwright per request with no limit (and its endpoint has no
   pool-full guard like the others) → burst load → OOM → driver death.
5. **Restart races.** `_periodic_cleanup` and any restart could interleave
   (double rebuild, stale pages re-queued).
6. **Small bugs:** `page.route(..., lambda route: route.fulfill(...))`
   passed a sync lambda returning a never-awaited coroutine;
   `page.eval_on_selector` is removed in current Playwright;
   `f"{scheme}://{host}:{parsed.port}"` produced `:None` for proxies
   without an explicit port; `_shutdown` crashed when startup had failed.

## Fixes applied

- `_is_driver_dead_error()` classifier + `_handle_driver_crash()`: on a
  dead-driver error the server **restarts the browser automatically** and the
  task returns `value: "driver_restarted"` (“please retry”) — one request
  fails instead of the whole server dying.
- `_get_page(timeout=60)`: raises `_PoolExhausted` (`value: "server_busy"`)
  instead of hanging forever; all three pool solvers use it.
- `_return_page()`: recycles a page only if its context is registered **and**
  passes a cheap `context.cookies()` liveness probe; otherwise closes it.
- Turnstile rotation now unregisters the old context and registers the new
  one (leak closed); route handler is a real `async def`; styling via
  `locator(...).evaluate(...)` in try/except.
- `_solve_recaptcha` runs under a semaphore sized to pool capacity
  (waits ≤60 s, else `server_busy`); semaphore released in `finally`.
- `_pool_lock` + `_restarting` serialize `_restart_browser()` against the
  periodic forced cleanup (which now reuses `_restart_contexts_locked()`);
  a cleanup that loses the race closes its stale pages instead of
  re-queueing them.
- `_proxy_server()` defaults ports (80/443) and is shared by both proxy
  paths; `_shutdown` null-guarded; new `GET /health` returns
  `{status, browser_connected, pool, capacity}` for monitoring.

## Verify

```bash
python3 -m py_compile api_server.py
python3 test_robustness.py   # 16 passed, 0 failed (mocked, no browser)
```

Live check still recommended on a VPS (needs `camoufox fetch` +
`playwright install-deps`): run with a small pool (`thread: 1–2`,
`page_count: 1`), hit `/health`, solve a few Turnstile/clearance tasks, and
confirm that killing the browser process yields one `driver_restarted`
response followed by successful solves — previously this required a manual
restart. Watch RAM: each context is ~300 MB; the shipped `config.json`
(5×2 = 10 slots ≈ 3 GB) is aggressive for a 2 GB VPS and was likely part of
the OOM story — consider lowering defaults to 2×1.

## Suggested upstream PR

PR title: “Self-healing browser pool: fix #1 (Connection closed…)”.
Body: root causes 1–6 above + `test_robustness.py` results. Keep #2 closed
as out-of-scope (paid third-party promo, no code).
