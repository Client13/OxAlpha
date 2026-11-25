#!/usr/bin/env python3
"""
main.py — all-in-one terminal chat for Ox Alpha with fully-enabled solver.

Single file, stdlib only. Run from the workspace root:

    python3 main.py                 # REPL: solver boots itself, challenges solve
    python3 main.py --once "hello"  # one question, print answer, exit

What it does, in order:
  1. SOLVER-FIRST: checks http://127.0.0.1:8001, auto-launches the local
     boterdrop-solver checkout when missing (clones it, pip-installs its
     deps, answers its setup prompt, waits for /health).
  2. CHAT: session to https://oxalpha.com/chat (cookies + CSRF), streams
     POST /api/chat SSE tokens to your terminal.
  3. On every Turnstile challenge: solves via the solver and retries with
     X-Turnstile-Token — the message is never lost.

Flags: --solver URL | --no-solver | --no-autostart | --system TEXT | --once MSG
Env: SOLVER_BASE SOLVER_DIR SOLVER_AUTOSTART SOLVER_AUTOCLONE SOLVER_SKIP_DEPS
     SOLVER_TIMEOUT SOLVER_START_TIMEOUT
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# ------------------------------------------------------------------ config ---
BASE = "https://oxalpha.com"
UPSTREAM_MODEL = "z-ai/glm-5.3-flash"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
MIN_INTERVAL = 2.0
OVERLOAD_RETRIES = 3
OVERLOAD_BACKOFF = [5, 15, 45]

TURNSTILE_SITEKEY = "0x4AAAAAABp_UYRVl7snkQ51"
TURNSTILE_URL = "https://oxalpha.com/chat/"
DEFAULT_SOLVER = "http://127.0.0.1:8001"
SOLVER_BASE = os.environ.get("SOLVER_BASE", DEFAULT_SOLVER).rstrip("/")
SOLVER_TIMEOUT = int(os.environ.get("SOLVER_TIMEOUT", "150"))
SOLVER_REPO = "https://github.com/najibyahya/Boterdrop-Solver"
SOLVER_AUTOSTART = os.environ.get("SOLVER_AUTOSTART", "1").lower() not in (
    "0", "false", "no", "off")
SOLVER_AUTOCLONE = os.environ.get("SOLVER_AUTOCLONE", "1").lower() not in (
    "0", "false", "no", "off")
SOLVER_START_TIMEOUT = int(os.environ.get("SOLVER_START_TIMEOUT", "600"))
_solver_proc = None
_solver_note = ""


# ------------------------------------------------------- solver HTTP client ---
class SolverError(Exception):
    def __init__(self, message, code="solver_error"):
        super().__init__(message)
        self.message = message
        self.code = code


def _solver_get(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": "main-chat/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8", "replace") or "{}")
        except Exception:
            body = {"message": f"solver HTTP {e.code}"}
        return e.code, body


def solver_health(base, timeout=10):
    try:
        status, body = _solver_get(base.rstrip("/") + "/health", timeout=timeout)
        if status == 200 and isinstance(body, dict):
            return body
        return {"status": "unknown", "note": f"HTTP {status}"}
    except Exception as e:
        return {"status": "down", "note": str(e)[:200]}


def solve_turnstile(base, url, sitekey, action="chat", timeout=150, poll_interval=2.0):
    """Mint a Turnstile token via Boterdrop-Solver. Returns token or raises."""
    base = base.rstrip("/")
    qs = urllib.parse.urlencode({"url": url, "sitekey": sitekey, "action": action})
    try:
        status, created = _solver_get(f"{base}/turnstile?{qs}", timeout=30)
    except Exception as e:
        raise SolverError(f"solver unreachable at {base} ({e})", "solver_down") from e
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
            raise SolverError(f"solver timed out after {timeout}s", "timeout")
        time.sleep(poll_interval)
        status, res = _solver_get(poll_url, timeout=30)
        if status == 202 or (status == 200 and isinstance(res, dict)
                             and res.get("status") == "process"):
            continue
        if status == 404:
            raise SolverError("solver forgot the task — retry", "task_expired")
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


# ------------------------------------------------------- solver auto-boot ----
def find_solver_dir():
    cand = os.environ.get("SOLVER_DIR", "").strip()
    if cand:
        if os.path.isfile(os.path.join(cand, "api_server.py")):
            return cand
        print(f"(SOLVER_DIR={cand} has no api_server.py — unset it to allow auto-detect)",
              flush=True)
        return None
    here = os.path.dirname(os.path.abspath(__file__))
    for d in (os.path.join(here, "boterdrop-solver"),
              os.path.join(os.getcwd(), "boterdrop-solver")):
        if os.path.isfile(os.path.join(d, "api_server.py")):
            return d
    return None


def _clone_solver():
    if not SOLVER_AUTOCLONE:
        return None
    if not shutil.which("git"):
        print("(auto-clone skipped: git not installed)", flush=True)
        return None
    target = os.path.join(os.path.dirname(os.path.abspath(__file__)), "boterdrop-solver")
    if os.path.exists(target):
        return None
    print(f"(cloning solver to {target}…)", flush=True)
    try:
        r = subprocess.run(["git", "clone", "--depth", "1", SOLVER_REPO, target],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=180)
        if r.returncode == 0 and os.path.isfile(os.path.join(target, "api_server.py")):
            print("(clone ok)", flush=True)
            return target
        print("(clone failed: " + r.stdout.decode("utf-8", "replace")[-300:] + ")",
              flush=True)
    except Exception as e:
        print(f"(clone failed: {e})", flush=True)
    return None


def _log_tail(path, n=15):
    try:
        with open(path, "rb") as f:
            lines = f.read().splitlines()[-n:]
        return "\n".join("    | " + l.decode("utf-8", "replace") for l in lines)
    except Exception as e:
        return f"    | (could not read log: {e})"


def _hint_for_log(tail):
    t = tail.lower()
    if "modulenotfound" in t or "no module named" in t:
        return ("deps missing and auto-install failed — create a venv and install: "
                "pip install 'fastapi==0.95.2' uvicorn 'camoufox[fetch]' loguru psutil playwright")
    if "camoufox" in t and ("fetch" in t or "not found" in t or "version information" in t):
        return "browser data missing — run once: python3 -m camoufox fetch"
    if "install-deps" in t or "libnspr" in t or "libnss" in t or "depends" in t:
        return "system libs missing — run once: python3 -m playwright install-deps (may need sudo)"
    if "xvfb" in t or "display" in t:
        return "no display for headed mode — set headless=true in config.json or run under xvfb-run"
    if "address already in use" in t:
        return "port busy — something else took :8001 after our check; retry or free the port"
    return ""


SOLVER_DEPS = ["fastapi==0.95.2", "uvicorn", "camoufox[fetch]", "loguru",
               "psutil", "playwright"]


def _ensure_solver_deps(timeout=600):
    """api_server.py imports fastapi/etc at module top, so its own installer
    never runs when they're missing. Install into THIS interpreter first."""
    if os.environ.get("SOLVER_SKIP_DEPS", "").lower() in ("1", "true", "yes"):
        return True, "dep check skipped"
    if not os.environ.get("LD_PRELOAD"):
        os.environ["LD_PRELOAD"] = "/nix/store/sr4cnxyzx24ylxygfk7d81hy4791l8gm-expat-2.7.3/lib/libexpat.so.1"
    env = os.environ.copy()
    env["LD_PRELOAD"] = "/nix/store/sr4cnxyzx24ylxygfk7d81hy4791l8gm-expat-2.7.3/lib/libexpat.so.1"
    missing = []
    for pkg in SOLVER_DEPS:
        mod = pkg.split("==")[0].split("[")[0]
        try:
            subprocess.run([sys.executable, "-c", f"import {mod}"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=60, check=True, env=env)
        except Exception:
            missing.append(pkg)
    if not missing:
        return True, "deps ok"
    print(f"(installing missing solver deps: {', '.join(missing)}…)", flush=True)
    for pkg in missing:
        ok = False
        for extra in ([], ["--break-system-packages"]):
            try:
                subprocess.run([sys.executable, "-m", "pip", "install", pkg] + extra,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=timeout, check=True, env=env)
                ok = True
                break
            except Exception:
                continue
        if not ok:
            return False, (f"pip install {pkg} failed — try a venv: "
                           f"python3 -m venv venv && source venv/bin/activate && "
                           f"pip install {' '.join(SOLVER_DEPS)}")
        print(f"(installed {pkg})", flush=True)
    return True, "deps installed"


def _nix_lib_path():
    """LD_LIBRARY_PATH for the nix Firefox system libs (gtk3/nss closure).

    Cached in /tmp/nixlibs.env. Drops gcc runtimes — they crash the
    Playwright node driver with 'stack smashing' (the exact
    'Connection closed while reading from the driver' from issue #1)."""
    cache = "/tmp/nixlibs.env"
    try:
        if os.path.isfile(cache):
            raw = open(cache).read().strip().rstrip(":")
            dirs = [d for d in raw.split(":") if d and os.path.isdir(d)]
            if len(dirs) > 10:
                return ":".join(dirs) + ":"
    except Exception:
        pass
    import glob  # noqa: F401 (kept for fallback below)
    roots, seen_roots = [], set()
    try:
        r = subprocess.run(["nix", "profile", "list"],
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=60, text=True)
        for p in re.findall(r"/nix/store/[a-z0-9]+-[A-Za-z0-9+._-]+", r.stdout):
            if p not in seen_roots and not re.search(r"-(man|doc|dev|debug|bin)$", p):
                seen_roots.add(p)
                roots.append(p)
    except Exception:
        roots = []
    # The lib outputs of glib/dbus hide behind -bin entries in profile list;
    # pull the exact ones from gtk3's own runtime closure instead.
    try:
        g = subprocess.run(
            ["nix-store", "-qR",
             next(p for p in roots if re.search(r"gtk\+3-", p))],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=120, text=True)
        for p in g.stdout.split():
            if re.search(r"/(glib-2[\d.]+|dbus-1[\d.]+)(-lib)?$", p) \
                    and p not in seen_roots:
                seen_roots.add(p)
                roots.append(p)
    except Exception:
        pass
    if not roots:  # fallback: newest gtk3 + nss on disk
        cands = sorted(glob.glob("/nix/store/*-gtk+3-3.24.49")) + \
            sorted(glob.glob("/nix/store/*-nss-3.11*"))
        roots = [d for d in cands if os.path.isdir(d)
                 and not d.endswith(("-man", "-doc", "-dev", "-bin", "-debug",
                                     "-tools"))]
    if not roots:
        return ""
    try:
        r = subprocess.run(["nix-store", "-qR"] + roots,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           timeout=120, text=True)
        seen, out = set(), []
        for p in r.stdout.split():
            lib = os.path.join(p, "lib")
            if os.path.isdir(lib) and lib not in seen:
                if any(os.path.exists(os.path.join(lib, f))
                       for f in ("libstdc++.so.6", "libgcc_s.so.1")):
                    continue  # poison for the node driver — skip
                seen.add(lib)
                out.append(lib)
        raw = ":".join(out) + (":" if out else "")
        try:
            open(cache, "w").write(raw)
        except Exception:
            pass
        return raw
    except Exception:
        return ""


def _launch_solver(sdir, port):
    global _solver_proc
    log_path = os.path.join(sdir, "solver_autostart.log")
    ok, note = _ensure_solver_deps()
    if not ok:
        print(f"(auto-start failed: {note})", flush=True)
        return False
    # The checkout's config.json may point at another port — align it with
    # the port we are about to poll so solver and chat agree.
    cfg_path = os.path.join(sdir, "config.json")
    try:
        with open(cfg_path) as f:
            cfg = json.load(f)
        if cfg.get("port") != port:
            cfg["port"] = port
            with open(cfg_path, "w") as f:
                json.dump(cfg, f, indent=4)
            print(f"(solver config port set to {port})", flush=True)
    except Exception as e:
        print(f"(could not adjust solver config port: {e})", flush=True)
    nixlibs = _nix_lib_path()
    if nixlibs:
        os.environ["LD_LIBRARY_PATH"] = nixlibs + os.environ.get("LD_LIBRARY_PATH", "")
        print("(nix system libs wired for the browser)", flush=True)
    # Headed solvers need a display: wrap with xvfb-run when headless=false
    # and no DISPLAY (typical VPS). Turnstile trusts headed far more.
    cmd = [sys.executable, "api_server.py"]
    try:
        with open(os.path.join(sdir, "config.json")) as f:
            headed = not json.load(f).get("headless", True)
    except Exception:
        headed = False
    if headed and not os.environ.get("DISPLAY"):
        os.environ["PATH"] = os.path.expanduser("~/.nix-profile/bin") + ":" + \
            os.environ.get("PATH", "")
        if shutil.which("xvfb-run"):
            cmd = ["xvfb-run", "-a"] + cmd
            print("(headless=false detected — wrapping solver in xvfb-run)",
                  flush=True)
        else:
            print("(headless=false but no xvfb-run found — browser may fail; "
                  "nix profile install xorg.xvfb)", flush=True)
    ok, note = _ensure_solver_deps()
    if not ok:
        print(f"(auto-start failed: {note})", flush=True)
        return False
    print(f"(starting solver from {sdir}; log: {log_path})", flush=True)
    try:
        logf = open(log_path, "ab")
        env = os.environ.copy()
        env["LD_PRELOAD"] = "/nix/store/sr4cnxyzx24ylxygfk7d81hy4791l8gm-expat-2.7.3/lib/libexpat.so.1"
        proc = subprocess.Popen(
            cmd, cwd=sdir,
            stdin=subprocess.PIPE, stdout=logf, stderr=subprocess.STDOUT,
            start_new_session=True, env=env)
    except Exception as e:
        print(f"(auto-start failed to launch: {e})", flush=True)
        return False
    try:
        proc.stdin.write(b"\n")  # answer "Lanjutkan? [Enter/Y]" non-interactively
        proc.stdin.flush()
    except Exception:
        pass
    deadline = time.time() + SOLVER_START_TIMEOUT
    while time.time() < deadline:
        if proc.poll() is not None:
            tail = _log_tail(log_path)
            print(f"(auto-start failed: solver exited code {proc.returncode})",
                  flush=True)
            print(tail, flush=True)
            hint = _hint_for_log(tail)
            if hint:
                print(f"(fix: {hint})", flush=True)
            return False
        try:
            if solver_health(SOLVER_BASE, timeout=5).get("status") == "ok":
                _solver_proc = proc
                print(f"(solver auto-started at {SOLVER_BASE}, pid {proc.pid}; "
                      f"keeps running after chat exits)", flush=True)
                return True
        except Exception:
            pass
        time.sleep(3)
    tail = _log_tail(log_path)
    print(f"(auto-start timed out after {SOLVER_START_TIMEOUT}s)", flush=True)
    print(tail, flush=True)
    hint = _hint_for_log(tail)
    if hint:
        print(f"(fix: {hint})", flush=True)
    return False


def ensure_solver(autostart=True):
    """Boot the solver FIRST; True when solving will work. Never raises."""
    global _solver_note
    if not SOLVER_BASE:
        _solver_note = "disabled (--no-solver)"
        return False
    try:
        if solver_health(SOLVER_BASE, timeout=8).get("status") == "ok":
            _solver_note = f"ready at {SOLVER_BASE}"
            print(f"(solver {_solver_note})", flush=True)
            return True
    except Exception:
        pass
    if not autostart or not SOLVER_AUTOSTART:
        _solver_note = "not running and auto-start off"
        return False
    sdir = find_solver_dir()
    if not sdir and not os.environ.get("SOLVER_DIR", "").strip():
        sdir = _clone_solver()
    port = solver_port()
    if not sdir:
        _solver_note = "no checkout and clone failed — start it manually"
        print("(auto-start skipped: no boterdrop-solver found)", flush=True)
        return False
    if port != 8001:
        _solver_note = f"SOLVER_BASE port is {port}, checkout serves :8001"
        print(f"(auto-start skipped: {_solver_note})", flush=True)
        return False
    with socket.socket() as s:
        s.settimeout(2)
        if s.connect_ex(("127.0.0.1", port)) == 0:
            _solver_note = f"port {port} busy but not a solver"
            print(f"(auto-start skipped: {_solver_note})", flush=True)
            return False
    ok = _launch_solver(sdir, port)
    _solver_note = f"auto-started at {SOLVER_BASE}" if ok else "auto-start failed"
    return ok


def solver_port():
    try:
        return urllib.parse.urlparse(SOLVER_BASE).port or 80
    except Exception:
        return None


def try_auto_solve():
    if not SOLVER_BASE:
        return None
    print(f"(auto-solving Turnstile via {SOLVER_BASE}…)", flush=True)
    try:
        token = solve_turnstile(SOLVER_BASE, TURNSTILE_URL, TURNSTILE_SITEKEY,
                                action="chat", timeout=SOLVER_TIMEOUT)
    except SolverError as e:
        if getattr(e, "code", "") == "solver_down":
            print(f"(no solver running at {SOLVER_BASE}"
                  + (f" — {_solver_note}" if _solver_note else "") + ")",
                  flush=True)
        else:
            print(f"(auto-solve failed: {e.message})", flush=True)
        return None
    except Exception as e:
        print(f"(auto-solve failed: {e})", flush=True)
        return None
    print("(solver returned a token, retrying…)", flush=True)
    return token


# ------------------------------------------------------------- chat client ---
class OxError(Exception):
    def __init__(self, message, code="upstream_error", status=502):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


class OxSession:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))
        self.csrf = ""
        self.last_ended = 0.0

    def init(self):
        req = urllib.request.Request(BASE + "/chat", headers={"User-Agent": UA})
        with self.opener.open(req, timeout=30) as r:
            html = r.read().decode("utf-8", "replace")
        m = re.search(r'<meta name="csrf-token" content="([^"]+)"', html)
        if not m:
            raise OxError("could not extract csrf-token from /chat", "init_failed")
        self.csrf = m.group(1)

    def _pace(self):
        wait = MIN_INTERVAL - (time.time() - self.last_ended)
        if wait > 0:
            time.sleep(wait)

    def generate(self, messages, turnstile_token=None, timeout=180):
        """POST /api/chat. Yields ("text", s) / ("usage", d)."""
        self._pace()
        body = json.dumps({"model": UPSTREAM_MODEL, "messages": messages}).encode()
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream",
                   "X-CSRF-TOKEN": self.csrf, "Origin": BASE,
                   "Referer": BASE + "/chat", "User-Agent": UA}
        if turnstile_token:
            headers["X-Turnstile-Token"] = turnstile_token

        def _post(hdrs):
            req = urllib.request.Request(BASE + "/api/chat", data=body, headers=hdrs)
            return self.opener.open(req, timeout=timeout)

        try:
            resp = _post(headers)
        except urllib.error.HTTPError as e:
            if e.code == 419:
                self.init()
                headers["X-CSRF-TOKEN"] = self.csrf
                try:
                    resp = _post(headers)
                except urllib.error.HTTPError as e2:
                    raise self._http_to_err(e2) from e2
            else:
                raise self._http_to_err(e) from e

        ctype = resp.headers.get("Content-Type", "")
        if "application/json" in ctype:
            try:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
            except Exception:
                payload = {"error": "unexpected upstream reply"}
            raise self._payload_to_err(resp.status, payload)

        buf = b""
        try:
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    ev = self._parse_line(line.decode("utf-8", "replace").strip())
                    if ev:
                        yield ev
        finally:
            try:
                resp.close()
            except Exception:
                pass
            self.last_ended = time.time()

    @staticmethod
    def _http_to_err(e):
        try:
            payload = json.loads(e.read().decode("utf-8", "replace") or "{}")
        except Exception:
            payload = {"error": f"upstream HTTP {e.code}"}
        return OxSession._payload_to_err(e.code, payload)

    @staticmethod
    def _payload_to_err(status, payload):
        if not isinstance(payload, dict):
            return OxError(str(payload)[:500], "upstream_error", status)
        code = str(payload.get("code", ""))
        msg = str(payload.get("error", payload.get("message", "upstream error")))[:500]
        if code == "turnstile_required" or status == 428:
            return OxError(msg, "turnstile_required", 429)
        if status == 429 or "overload" in msg.lower() or "limit" in msg.lower():
            return OxError(msg, "overloaded", 429)
        return OxError(msg, code or "upstream_error", status)

    @staticmethod
    def _parse_line(line):
        if line.startswith(":") or not line.startswith("data:"):
            return None
        data = line[5:].strip()
        if data == "[DONE]":
            return None
        try:
            obj = json.loads(data)
        except Exception:
            return None
        if "choices" not in obj:
            if isinstance(obj, dict) and ("status" in obj or "error" in obj):
                return ("error", json.dumps(obj)[:300])
            return None
        try:
            ch = obj["choices"][0]
            delta = ch.get("delta", {}) or {}
            if delta.get("content"):
                return ("text", delta["content"])
            if obj.get("usage"):
                return ("usage", obj["usage"])
        except Exception:
            pass
        return None


def ask_generation(sess, messages, turnstile_token=None):
    token = turnstile_token
    for attempt in range(OVERLOAD_RETRIES + 1):
        try:
            parts, usage = [], {}
            for kind, val in sess.generate(messages, turnstile_token=token):
                if kind == "text":
                    parts.append(val)
                    yield ("text", val)
                elif kind == "usage":
                    usage = val
            yield ("done", {"text": "".join(parts), "usage": usage})
            return
        except OxError as e:
            if e.code == "overloaded" and attempt < OVERLOAD_RETRIES:
                wait = OVERLOAD_BACKOFF[min(attempt, len(OVERLOAD_BACKOFF) - 1)]
                yield ("retry", f"upstream busy, retrying in {wait}s…")
                time.sleep(wait)
                continue
            yield ("failed", e)
            return


def turnstile_prompt(auto_tried):
    print("\n--- upstream asked for human verification (Cloudflare Turnstile) ---")
    if auto_tried:
        print("  (auto-solve didn't clear it — retrying manually.)")
    elif _solver_note:
        print(f"  (solver status: {_solver_note})")
    print("  1. Open https://oxalpha.com/chat in your browser, solve the check,")
    print("     come back and press Enter to retry; or")
    print("  2. Paste a token after 't '; or  3. Type q to give up.")
    try:
        ans = input("[Enter = retry | t <token> | q] ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if ans.lower().startswith("t ") and len(ans) > 2:
        return ("token", ans[2:].strip())
    if ans.lower() in ("q", "quit"):
        return None
    return ("retry", None)


def stream_with_recovery(sess, msgs):
    t_token, attempt, auto_tried = None, 0, False
    while True:
        attempt += 1
        text_parts, failed = [], None
        try:
            for kind, val in ask_generation(sess, msgs, t_token):
                if kind == "text":
                    text_parts.append(val)
                    print(val, end="", flush=True)
                elif kind == "retry":
                    print(f"\n({val})", flush=True)
                elif kind == "failed":
                    failed = val
                    break
                elif kind == "done":
                    print()
                    return val["text"]
        except KeyboardInterrupt:
            print("\n(cancelled)")
            return "".join(text_parts) or None
        if failed is None:
            return "".join(text_parts)
        if failed.code == "turnstile_required":
            try:
                sess.init()
            except Exception:
                pass
            if attempt > 3:
                print("\n(still challenged after 3 tries — try again later)")
                return None
            if not auto_tried and SOLVER_BASE:
                auto_tried = True
                t_token = try_auto_solve()
                if t_token:
                    continue
            decision = turnstile_prompt(auto_tried=t_token is not None)
            if decision is None:
                print("(dropped that message — /retry to try again later)")
                return None
            if decision[0] == "token":
                t_token = decision[1]
            continue
        print(f"\n(error: {failed.message})")
        return "".join(text_parts) or ""


def run_repl(sess):
    history, system = [], None
    print("Ox Alpha terminal chat — /help /new /retry /system <t> /save <f> /quit")
    while True:
        try:
            user = input("\n> ").strip()
        except EOFError:
            print("\nbye.")
            return
        except KeyboardInterrupt:
            print("\n(type /quit to exit)")
            continue
        if not user:
            continue
        if user.startswith("/"):
            cmd, _, arg = user[1:].partition(" ")
            if cmd in ("quit", "exit", "q"):
                print("bye.")
                return
            if cmd == "help":
                print("/new reset | /retry resend | /system <t> | /save <f> | /quit")
                continue
            if cmd == "new":
                history.clear()
                print("(fresh context)")
                continue
            if cmd == "retry":
                if not history or history[-1]["role"] != "user":
                    print("(nothing to retry)")
                    continue
                user = history.pop()["content"]
            elif cmd == "system":
                system = arg.strip() or None
                print(f"(system {'set' if system else 'cleared'})")
                continue
            elif cmd == "save":
                path = arg.strip() or "chat_transcript.md"
                with open(path, "w") as f:
                    for m in history:
                        f.write(f"**{m['role']}:** {m['content']}\n\n")
                print(f"(saved to {path})")
                continue
            else:
                print("(unknown command)")
                continue
        msgs = ([{"role": "system", "content": system}] if system else []) + history + [
            {"role": "user", "content": user}]
        history.append({"role": "user", "content": user})
        assistant = stream_with_recovery(sess, msgs)
        if assistant is None:
            history.pop()
        elif assistant:
            history.append({"role": "assistant", "content": assistant})


def main():
    global BASE, SOLVER_BASE, SOLVER_TIMEOUT, SOLVER_AUTOSTART
    ap = argparse.ArgumentParser(description="Ox Alpha all-in-one terminal chat + solver")
    ap.add_argument("--once", metavar="MSG", help="send one message, print reply, exit")
    ap.add_argument("--solver", metavar="URL", default=None)
    ap.add_argument("--no-solver", action="store_true")
    ap.add_argument("--no-autostart", action="store_true")
    ap.add_argument("--system", default=None)
    ap.add_argument("--base", default=BASE)
    args = ap.parse_args()
    BASE = args.base.rstrip("/")
    if args.no_solver:
        SOLVER_BASE = ""
    elif args.solver:
        SOLVER_BASE = args.solver.rstrip("/")
    if args.no_autostart:
        SOLVER_AUTOSTART = False

    sess = OxSession()
    try:
        sess.init()
    except OxError as e:
        print(f"could not start upstream session: {e.message}", file=sys.stderr)
        return 1
    print(f"(session ready, model {UPSTREAM_MODEL})")
    if SOLVER_BASE:
        print(f"(solver: {SOLVER_BASE} — booting first…) ")
        ensure_solver()

    if args.once:
        msgs = ([{"role": "system", "content": args.system}] if args.system else []) + [
            {"role": "user", "content": args.once}]
        fail, tried, token = None, False, None
        while True:
            for kind, val in ask_generation(sess, msgs, token):
                if kind == "text":
                    print(val, end="", flush=True)
                elif kind == "retry":
                    print(f"({val})", file=sys.stderr, flush=True)
                elif kind == "failed":
                    fail = val
                    break
                elif kind == "done":
                    print()
                    return 0
            if fail is None:
                return 0
            if fail.code == "turnstile_required" and SOLVER_BASE and not tried:
                tried = True
                try:
                    sess.init()
                except Exception:
                    pass
                token = try_auto_solve()
                if token:
                    fail = None
                    continue
            break
        print(f"error [{fail.code}]: {fail.message}", file=sys.stderr)
        return 1
    run_repl(sess)
    return 0


if __name__ == "__main__":
    sys.exit(main())
