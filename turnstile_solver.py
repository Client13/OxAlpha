#!/usr/bin/env python3
"""
turnstile_solver.py — Cloudflare Turnstile token minting for the OxAlpha proxy.

Two interchangeable backends, selected via SOLVER_MODE / automatic fallback:

1. "builtin"  — a local headless-Chromium solver built on Playwright.
                Same trick the browser site uses: serve a minimal page AT the
                protected origin (route interception), embed the Turnstile
                widget with the target sitekey, wait for the response token,
                click the widget if the interactive checkbox appears.
                Needs:  pip install playwright && playwright install chromium
                (No camoufox — the old camoufox sidecar was the #1 reason the
                original project never booted: heavy download, module-level
                import before its own auto-installer, Replit-only paths.)

2. "external" — any Boterdrop-Solver-compatible sidecar speaking the
                /turnstile -> task_id, /result?id= polling protocol
                (e.g. github.com/najibyahya/Boterdrop-Solver on port 8001).

Both are optional: when the upstream does not issue a challenge, neither is
ever contacted. Tokens are single-use and expire (~300s), so they are minted
on demand only.

Stdlib-only except the builtin backend's optional playwright import.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>.</title>
<script src="https://challenges.cloudflare.com/turnstile/v0/api.js?onload=_cta" async defer></script>
</head><body>
<!--turnstile-->
</body></html>
"""


class SolverError(Exception):
    """Raised when a Turnstile token cannot be minted."""


# --------------------------------------------------------------- external ----

def _get_json(url: str, timeout: float = 30.0) -> tuple[int, dict]:
    req = urllib.request.Request(url, headers={"User-Agent": "oxalpha-proxy/2.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace") or "{}"
            return r.status, json.loads(raw)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", "replace") or "{}")
        except Exception:
            return e.code, {}
    except Exception as e:
        raise SolverError(f"{type(e).__name__}: {e}") from e


def external_health(base: str, timeout: float = 8.0) -> dict:
    try:
        status, body = _get_json(base.rstrip("/") + "/health", timeout=timeout)
        if status == 200 and isinstance(body, dict):
            return {"status": str(body.get("status", "ok")), "backend": "external"}
        return {"status": "down", "note": f"HTTP {status}"}
    except Exception as e:
        return {"status": "down", "note": str(e)[:200]}


def external_solve(base: str, url: str, sitekey: str, action: str | None = None,
                   timeout: float = 150.0, poll_interval: float = 2.0) -> str:
    """Mint a token through a Boterdrop-compatible sidecar. Returns the token."""
    base = base.rstrip("/")
    qs = urllib.parse.urlencode({"url": url, "sitekey": sitekey,
                                 **({"action": action} if action else {})})
    status, created = _get_json(f"{base}/turnstile?{qs}", timeout=30)
    task_id = created.get("task_id") if isinstance(created, dict) else None
    if status not in (200, 202) or not task_id:
        detail = (created.get("error") or created.get("message")
                  if isinstance(created, dict) else created)
        raise SolverError(f"external solver refused task (HTTP {status}): {detail}")

    deadline = time.time() + timeout
    poll_url = f"{base}/result?id=" + urllib.parse.quote(str(task_id))
    while True:
        if time.time() > deadline:
            raise SolverError(f"external solver timed out after {timeout:g}s")
        time.sleep(poll_interval)
        status, res = _get_json(poll_url, timeout=30)
        if status == 202 or (status == 200 and isinstance(res, dict)
                             and res.get("status") == "process"):
            continue
        if status == 404:
            raise SolverError("external solver forgot the task (expired) — retry")
        if status == 200 and isinstance(res, dict) and res.get("status") == "success":
            token = res.get("value") or res.get("token") or ""
            if token and token not in ("captcha_fail", "timeout", "driver_restarted"):
                return str(token)
        detail = res.get("message") or res.get("error") or res.get("value") if isinstance(res, dict) else res
        raise SolverError(f"external solver failed (HTTP {status}): {detail}")


# --------------------------------------------------------------- builtin -----

class BuiltinSolver:
    """Headless-Chromium Turnstile solver, one solve at a time.

    Engine preference: patchright (drop-in playwright fork with anti-detection
    patches — same engine as github.com/Theyka/Turnstile-Solver) falling back
    to plain playwright. On flagged/datacenter IPs Cloudflare may refuse to
    render the widget for ANY local browser; then use SOLVER_MODE=external
    with a Boterdrop-compatible sidecar or a residential IP.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._engine = "none"

    def _load_sync_api(self):
        """Return (sync_playwright_factory, engine_name) or raise SolverError."""
        try:
            from patchright.sync_api import sync_playwright
            self._engine = "patchright"
            return sync_playwright, self._engine
        except ImportError:
            pass
        try:
            from playwright.sync_api import sync_playwright
            self._engine = "playwright"
            return sync_playwright, self._engine
        except ImportError as e:
            raise SolverError(
                "builtin solver needs patchright (recommended) or playwright: "
                "pip install patchright && patchright install chromium") from e

    # -- availability probe (cheap, no browser launch) --
    def health(self) -> dict:
        try:
            _, engine = self._load_sync_api()
        except SolverError as e:
            return {"status": "unavailable", "note": str(e)}
        exe = self._chromium_path()
        if not exe:
            return {"status": "unavailable",
                    "note": "chromium not downloaded; run: patchright install chromium"}
        return {"status": "ok", "backend": "builtin", "engine": engine,
                "browser": os.path.basename(exe)}

    @staticmethod
    def _chromium_path() -> str | None:
        """Locate a downloaded chromium binary without launching anything."""
        import glob
        roots = [
            os.path.expanduser("~/.cache/ms-playwright"),
            os.path.expanduser("~/Library/Caches/ms-playwright"),
        ]
        if os.name == "nt":
            roots.append(os.path.expandvars(r"%LOCALAPPDATA%\ms-playwright"))
        patterns = [
            "chromium-*/chrome-linux*/chrome",
            "chromium-*/chrome-win*/chrome.exe",
            "chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium",
        ]
        for root in roots:
            if not os.path.isdir(root):
                continue
            for pat in patterns:
                hits = glob.glob(os.path.join(root, pat))
                if hits:
                    return hits[0]
        return None

    def solve(self, url: str, sitekey: str, action: str | None = None,
              timeout: float = 120.0) -> str:
        with self._lock:  # one browser at a time; solves are rare
            return self._solve_once(url, sitekey, action, timeout)

    def _solve_once(self, url: str, sitekey: str, action: str | None,
                    timeout: float) -> str:
        sync_playwright, engine = self._load_sync_api()
        if not self._chromium_path():
            raise SolverError("builtin solver needs a browser: patchright install chromium")

        url_slash = url if url.endswith("/") else url + "/"
        div = (f'<div class="cf-turnstile" style="background:#fff" '
               f'data-sitekey="{sitekey}"'
               + (f' data-action="{action}"' if action else "")
               + "></div>")
        html = _PAGE_TEMPLATE.replace("<!--turnstile-->", div)

        last_err = "no token observed"
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox",
                      "--disable-gpu", "--disable-dev-shm-usage"],
            )
            try:
                ctx = browser.new_context(user_agent=USER_AGENT,
                                          viewport={"width": 480, "height": 320})
                page = ctx.new_page()

                def _fulfill(route):
                    try:
                        route.fulfill(body=html, status=200,
                                      content_type="text/html; charset=utf-8")
                    except Exception:
                        pass

                page.route(url_slash, _fulfill)
                try:
                    page.goto(url_slash, wait_until="domcontentloaded", timeout=30000)
                except Exception as e:
                    last_err = f"page load failed: {e}"

                deadline = time.time() + timeout
                clicked = 0
                while time.time() < deadline:
                    try:
                        value = page.input_value("[name=cf-turnstile-response]",
                                                 timeout=500)
                        if value:
                            return value
                    except Exception:
                        pass
                    if clicked < 12:  # click periodically; widget may need it
                        try:
                            page.locator("div.cf-turnstile").click(timeout=400)
                            clicked += 1
                        except Exception:
                            pass
                    time.sleep(0.5)

                # diagnostics for the log
                try:
                    last_err = f"title='{page.title()}'"
                except Exception:
                    pass
                self._save_debug(page)
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
        raise SolverError(f"builtin solver: no token within {timeout:g}s ({last_err})")

    @staticmethod
    def _save_debug(page) -> None:
        try:
            dbg = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug_logs")
            os.makedirs(dbg, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            page.screenshot(path=os.path.join(dbg, f"{ts}_turnstile_fail.png"), full_page=True)
        except Exception:
            pass


# ------------------------------------------------------------ orchestrator ---

def health(mode: str = "auto", external_base: str = "") -> dict:
    """Combined solver health for /health reporting."""
    if mode == "off":
        return {"mode": "off", "status": "disabled",
                "note": "SOLVER_MODE=off — challenges surface as HTTP 429"}
    builtin = BuiltinSolver().health()
    external = external_health(external_base) if external_base else \
        {"status": "not-configured", "note": "SOLVER_BASE not set"}
    ok = {"builtin": builtin.get("status") == "ok",
          "external": external.get("status") == "ok"}
    if mode == "builtin":
        overall = "ok" if ok["builtin"] else builtin.get("status", "down")
        backend = "builtin"
    elif mode == "external":
        overall = "ok" if ok["external"] else external.get("status", "down")
        backend = "external"
    else:  # auto: either will do
        overall = "ok" if (ok["builtin"] or ok["external"]) else "down"
        backend = "builtin" if ok["builtin"] else ("external" if ok["external"] else "none")
    return {"mode": mode, "status": overall, "backend": backend,
            "builtin": builtin, "external": external}


def solve(url: str, sitekey: str, action: str | None = None,
          timeout: float = 150.0, mode: str = "auto", external_base: str = "") -> str:
    """Mint a token using the configured backend order. Raises SolverError."""
    order = {"auto": ("external", "builtin") if external_base else ("builtin",),
             "builtin": ("builtin",), "external": ("external",), "off": ()}[mode]
    errors = []
    for backend in order:
        try:
            if backend == "external":
                return external_solve(external_base, url, sitekey, action, timeout)
            return BuiltinSolver().solve(url, sitekey, action, timeout)
        except SolverError as e:
            errors.append(f"{backend}: {e}")
    raise SolverError("all solver backends failed — " + " | ".join(errors) if errors
                      else "solver disabled (SOLVER_MODE=off)")
