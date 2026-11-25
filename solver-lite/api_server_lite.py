#!/usr/bin/env python3
"""
solver-lite — UI-bridge for the OxAlpha web app (patchright + Chromium/Xvfb).

Instead of minting Turnstile tokens (Chromium gets the interactive challenge
and Camoufox OOM-kills 512MB hosts), this drives the REAL oxalpha.com/chat
page like a human user: new chat -> paste the composed conversation -> send
-> read the assistant bubble. The site handles CSRF/Turnstile itself, so the
whole token problem disappears.

HTTP API (consumed by oxalpha-proxy in UPSTREAM_MODE=ui):

    GET  /health -> {"status": "...", "mode": "ui-bridge", "page_ready": bool}
    POST /chat   {"messages": [{"role","content"}, ...], "timeout": 120}
                 -> 200 {"text": "..."} | 429 {"error": "..."} | 502 {"error": "..."}

Env:
  LITE_HOST=127.0.0.1  LITE_PORT=8001
  LITE_CHAT_TIMEOUT=150    (max seconds to wait for a full reply)
  LITE_STABLE_SECS=2.5     (reply considered done when text stable this long)
  LITE_HEADLESS=0          (0 = headed under Xvfb [recommended])
  LITE_PROFILE=/tmp/lite-profile
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import time

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from loguru import logger

LITE_HOST = os.environ.get("LITE_HOST", "127.0.0.1")
LITE_PORT = int(os.environ.get("LITE_PORT", "8001"))
CHAT_TIMEOUT = int(os.environ.get("LITE_CHAT_TIMEOUT", "150"))
STABLE_SECS = float(os.environ.get("LITE_STABLE_SECS", "2.5"))
HEADLESS = os.environ.get("LITE_HEADLESS", "0").lower() in ("1", "true", "yes")
PROFILE = os.environ.get("LITE_PROFILE") or f"/tmp/lite-profile-{os.getpid()}"
# Close the whole browser after every chat: a finished page still holds
# hundreds of MB and OOM-kills 512MB containers minutes later. Each request
# then pays ~10s extra page-load, but RAM stays bounded. Set 0 on fat hosts.
CLOSE_AFTER = os.environ.get("LITE_CLOSE_AFTER", "0").lower() in ("1", "true", "yes")

CHAT_URL = "https://oxalpha.com/chat/"
CHROME_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--no-first-run",
    "--no-default-browser-check",
    # small-container RAM discipline (Render free = 512MB):
    "--renderer-process-limit=1",
    "--disable-site-isolation-trials",
    "--js-flags=--max-old-space-size=192",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-sync",
    "--mute-audio",
    "--metrics-recording-only",
]

LIMIT_MARKERS = ("reached your daily limit", "rate limit", "too many requests",
                 "daily limit", "try again tomorrow")


def compose_text(messages: list[dict]) -> str:
    """Render the folded OpenAI conversation as one chat message."""
    msgs = [m for m in messages if isinstance(m, dict) and (m.get("content") or "").strip()]
    if len(msgs) == 1 and msgs[0].get("role") == "user":
        return msgs[0]["content"].strip()
    parts: list[str] = []
    for m in msgs:
        role = m.get("role", "user")
        content = (m.get("content") or "").strip()
        if role == "system":
            parts.append("[Instructions]\n" + content)
        elif role == "assistant":
            parts.append("[Your previous reply]\n" + content)
        else:
            parts.append("[User]\n" + content)
    parts.append("[Instructions]\nReply only with your next answer as the assistant. "
                 "Do not repeat the transcript.")
    return "\n\n".join(parts)


class UIBridge:
    def __init__(self):
        self.app = FastAPI()
        self.ctx = None
        self.page = None
        self.pw = None
        self.busy = asyncio.Lock()
        self.app.get("/health")(self.health)
        self.app.post("/chat")(self.chat)

    # ── lifecycle ─────────────────────────────────────────────────────────
    def _ready(self) -> bool:
        try:
            return bool(self.page is not None and not self.page.is_closed())
        except Exception:
            return False

    async def ensure_page(self):
        if self._ready():
            # make sure we are actually on the chat page
            try:
                if "oxalpha.com" in (self.page.url or ""):
                    return
            except Exception:
                pass
            try:
                await self.page.close()
            except Exception:
                pass
            self.page = None
        async with self.busy:
            if self._ready():
                return
            from patchright.async_api import async_playwright
            if self.pw is None:
                self.pw = await async_playwright().start()
            if self.ctx is None:
                logger.info("[Lite] Launching Chromium (patchright, persistent)...")
                try:
                    self.ctx = await self.pw.chromium.launch_persistent_context(
                        PROFILE, headless=HEADLESS, locale="en-US",
                        viewport={"width": 1000, "height": 700}, args=CHROME_ARGS)
                except Exception as e:
                    # stale profile lock (crashed previous browser) kills every
                    # retry: wipe the profile dir and try once more
                    logger.warning(f"[Lite] launch failed ({type(e).__name__}); wiping profile, retrying once")
                    shutil.rmtree(PROFILE, ignore_errors=True)
                    self.ctx = await self.pw.chromium.launch_persistent_context(
                        PROFILE, headless=HEADLESS, locale="en-US",
                        viewport={"width": 1000, "height": 700}, args=CHROME_ARGS)
            self.page = self.ctx.pages[0] if self.ctx.pages else await self.ctx.new_page()
            logger.info("[Lite] Navigating to oxalpha chat...")
            await self.page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(2)
            logger.success("[Lite] Chat page ready")

    async def close_all(self):
        """Tear the whole context/browser down to free RAM (CLOSE_AFTER=1)."""
        try:
            if self.ctx is not None:
                await self.ctx.close()
        except Exception:
            pass
        self.ctx = None
        self.page = None
        logger.success("[Lite] browser closed after chat (RAM freed)")

    async def recover_page(self):
        """Hard reset: close and relaunch the page."""
        logger.warning("[Lite] Recovering chat page...")
        try:
            if self.page is not None:
                await self.page.close()
        except Exception:
            pass
        self.page = None
        await self.ensure_page()

    # ── the chat round-trip ───────────────────────────────────────────────
    async def do_chat(self, text: str, timeout: int) -> str:
        # Reload per request: keeps the page's DOM/JS memory bounded on small
        # hosts (a long-lived chat page accumulates hundreds of MB).
        try:
            if "oxalpha.com" not in (self.page.url or ""):
                await self.page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=60000)
            else:
                await self.page.reload(wait_until="domcontentloaded", timeout=60000)
        except Exception:
            await self.recover_page()
        await self.ensure_page()
        page = self.page
        await page.click(".new-chat-btn")
        await asyncio.sleep(0.8)
        ta = page.locator("textarea").first
        await ta.click()
        await ta.fill(text)
        await asyncio.sleep(0.3)
        await page.locator(".send-btn").click()
        sent_at = time.time()

        deadline = time.time() + timeout
        stable_since = None
        last_text = ""
        last_click = 0.0
        # Wait for the assistant bubble to appear, then stabilize.
        while time.time() < deadline:
            state = await page.evaluate(
                """() => {
                    const msgs = [...document.querySelectorAll('.msg')];
                    const asst = msgs.filter(m => m.className.includes('msg-assistant'));
                    const last = asst.length ? asst[asst.length-1] : null;
                    return {
                        n: asst.length,
                        text: last ? (last.innerText || '').trim() : '',
                        busy: !!document.querySelector('.send-btn.typing, [class*="stop"], [class*="loading"]')
                    };
                }""")
            txt = (state.get("text") or "").strip()
            if txt:
                low = txt.lower()
                if ("waiting for verification" in low or "verify" in low) and len(txt) < 80:
                    # The site popped its Turnstile modal — click the checkbox
                    # inside the CF iframe until it clears (throttled).
                    now = time.time()
                    if now - last_click > 2.5:
                        last_click = now
                        try:
                            fl = page.frame_locator(
                                "iframe[title='Widget containing a Cloudflare security challenge'], "
                                "iframe[src*='challenges.cloudflare.com']")
                            await fl.locator("input[type='checkbox']").first.click(timeout=2000)
                        except Exception:
                            pass
                    await asyncio.sleep(0.6)
                    continue
                if txt == last_text:
                    if stable_since is None:
                        stable_since = time.time()
                    elif time.time() - stable_since >= STABLE_SECS:
                        return txt
                else:
                    last_text = txt
                    stable_since = None
            else:
                # challenge may appear before any bubble text — try the modal too
                now = time.time()
                if now - last_click > 2.5:
                    last_click = now
                    try:
                        fl = page.frame_locator(
                            "iframe[title='Widget containing a Cloudflare security challenge'], "
                            "iframe[src*='challenges.cloudflare.com']")
                        await fl.locator("input[type='checkbox']").first.click(timeout=1500)
                    except Exception:
                        pass
            await asyncio.sleep(0.6)
        if last_text:
            return last_text  # best effort: return what we have
        raise TimeoutError(f"no assistant reply within {timeout}s")

    # ── HTTP ──────────────────────────────────────────────────────────────
    async def health(self):
        return JSONResponse(content={
            "status": "ok" if self._ready() else "standby",
            "mode": "ui-bridge",
            "page_ready": self._ready(),
        })

    async def chat(self, request: Request):
        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail={"error": "invalid JSON"})
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            raise HTTPException(status_code=400, detail={"error": "messages required"})
        timeout = int(payload.get("timeout") or CHAT_TIMEOUT)
        timeout = max(20, min(timeout, CHAT_TIMEOUT))
        text = compose_text(messages)
        t0 = time.time()
        try:
            async with self.busy:  # one conversation at a time
                reply = await asyncio.wait_for(self.do_chat(text, timeout), timeout=timeout + 20)
        except asyncio.TimeoutError:
            logger.warning("[Lite] chat timed out; scheduling page recovery")
            asyncio.create_task(self.recover_page())
            return JSONResponse(content={"error": f"chat timed out after {timeout}s"},
                                status_code=504)
        except Exception as e:
            logger.error(f"[Lite] chat failed: {type(e).__name__}: {e}")
            asyncio.create_task(self.recover_page())
            if CLOSE_AFTER:
                await self.close_all()
            return JSONResponse(content={"error": f"{type(e).__name__}: {e}"[:300]},
                                status_code=502)
        if CLOSE_AFTER:
            await self.close_all()
        elapsed = round(time.time() - t0, 2)
        low = reply.lower()
        if any(m in low for m in LIMIT_MARKERS) and len(reply) < 200:
            logger.warning(f"[Lite] upstream limit ({elapsed}s): {reply[:80]!r}")
            return JSONResponse(content={"error": "upstream daily/rate limit",
                                         "detail": reply[:150]}, status_code=429)
        logger.success(f"[Lite] chat ok ({elapsed}s, {len(reply)} chars)")
        return JSONResponse(content={"text": reply, "elapsed": elapsed})


def main():
    import sys
    logger.remove()
    logger.add(sys.stderr, level="INFO")
    bridge = UIBridge()

    @bridge.app.on_event("startup")
    async def _startup():
        # Warm the page in the background so the first request is fast.
        async def _warm():
            try:
                await bridge.ensure_page()
                logger.success("[Lite] warm-up complete")
            except Exception as e:
                logger.warning(f"[Lite] warm-up failed ({e}); will retry on demand")
        asyncio.create_task(_warm())

    uvicorn.run(bridge.app, host=LITE_HOST, port=LITE_PORT, log_level="warning")


if __name__ == "__main__":
    main()
