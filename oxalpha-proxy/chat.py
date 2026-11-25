#!/usr/bin/env python3
"""
Terminal chat for the free Ox Alpha web backend (https://oxalpha.com/chat).

Stdlib only — no pip install. Demonstrates everything learned while
reverse-engineering the site (see README.md), including handling for the exact
problem our proxy hit: after a few messages the upstream answers

    {"code": "turnstile_required", ...}   (or HTTP 428 / 429 when overloaded)

instead of tokens. This client deals with that interactively rather than
crashing:

  * keeps one CookieJar session (ox_alpha_session + XSRF-TOKEN) + CSRF token
    from GET /chat, refreshes it on HTTP 419 or when challenged;
  * paces requests and backs off on 429-overloaded with retries;
  * solver-FIRST startup: on launch it checks for a Boterdrop-Solver at
    http://127.0.0.1:8001 and auto-starts a local checkout if missing, so the
    first challenge solves instead of failing;
  * on every later turnstile_required it solves on demand through that solver
    (--no-solver / --no-autostart to opt out), falling back to an interactive
    manual recovery — the queued message is never lost.

Usage:
    python3 chat.py                        # interactive REPL (direct mode)
    python3 chat.py --once "hello"         # single question, print answer
    python3 chat.py --proxy http://127.0.0.1:8000/v1   # via oxalpha-openai-proxy
    python3 chat.py --no-solver            # never auto-solve, always ask

REPL commands: /help /new /retry /system <text> /save <file> /quit
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
import urllib.parse
import urllib.request

try:
    from turnstile_solver import SolverError, solve_turnstile, solver_health
except ImportError:
    class SolverError(Exception):  # type: ignore
        def __init__(self, message, code="solver_error"):
            super().__init__(message)
            self.message = message
            self.code = code

    def solve_turnstile(*a, **k):
        raise SolverError("turnstile_solver.py not found next to chat.py")

    def solver_health(base, timeout=10):
        return {"status": "down", "note": "turnstile_solver.py missing"}

BASE = "https://oxalpha.com"
UPSTREAM_MODEL = "z-ai/glm-5.3-flash"
MODEL_ALIASES = {"ox-alpha": UPSTREAM_MODEL, "stealth/ox-alpha": UPSTREAM_MODEL,
                 UPSTREAM_MODEL: UPSTREAM_MODEL}
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
MIN_INTERVAL = 2.0          # client-side pacing between generations
OVERLOAD_RETRIES = 3        # auto-retries on 429-overloaded
OVERLOAD_BACKOFF = [5, 15, 45]
# Turnstile auto-solve target: must match the widget oxalpha.com renders
# (sitekey from /chat, action 'chat'). ON BY DEFAULT at the standard
# Boterdrop-Solver address — override with --solver / SOLVER_BASE, disable
# with --no-solver or SOLVER_BASE="".
TURNSTILE_SITEKEY = "0x4AAAAAABp_UYRVl7snkQ51"
TURNSTILE_URL = "https://oxalpha.com/chat/"
DEFAULT_SOLVER = "http://127.0.0.1:8001"
SOLVER_BASE = os.environ.get("SOLVER_BASE", DEFAULT_SOLVER).rstrip("/")
SOLVER_TIMEOUT = int(os.environ.get("SOLVER_TIMEOUT", "150"))
# Solver-first startup: boot a local Boterdrop-Solver automatically when none
# answers. 0/False disables (env SOLVER_AUTOSTART or flag --no-autostart).
SOLVER_AUTOSTART = os.environ.get("SOLVER_AUTOSTART", "1").lower() not in (
    "0", "false", "no", "off")
SOLVER_START_TIMEOUT = int(os.environ.get("SOLVER_START_TIMEOUT", "300"))
# If no checkout exists anywhere, clone it (public OSS repo). Off with
# SOLVER_AUTOCLONE=0. Target dir: next to chat.py when writable, else cwd.
SOLVER_REPO = "https://github.com/najibyahya/Boterdrop-Solver"
SOLVER_AUTOCLONE = os.environ.get("SOLVER_AUTOCLONE", "1").lower() not in (
    "0", "false", "no", "off")
_solver_proc = None
_solver_note = ""  # short human-readable solver status, shown in prompts


def find_solver_dir():
    """Locate a local boterdrop-solver checkout (SOLVER_DIR or next to us)."""
    cand = os.environ.get("SOLVER_DIR", "").strip()
    if cand:
        if os.path.isfile(os.path.join(cand, "api_server.py")):
            return cand
        print(f"(SOLVER_DIR={cand} has no api_server.py — not auto-starting; "
              f"unset it to allow auto-detect)", flush=True)
        return None
    here = os.path.dirname(os.path.abspath(__file__))
    parent = os.path.dirname(here)
    for d in (os.path.join(here, "boterdrop-solver"),
              os.path.join(parent, "boterdrop-solver"),
              os.path.join(os.path.dirname(parent), "boterdrop-solver"),
              os.path.join(os.getcwd(), "boterdrop-solver")):
        if os.path.isfile(os.path.join(d, "api_server.py")):
            return d
    return None


def _clone_solver():
    """git-clone the solver checkout next to the suite (or cwd). Returns dir or None."""
    if not SOLVER_AUTOCLONE:
        return None
    if not shutil.which("git"):
        print("(auto-clone skipped: git not installed)", flush=True)
        return None
    here = os.path.dirname(os.path.abspath(__file__))
    target = os.path.join(os.path.dirname(here), "boterdrop-solver")
    if not os.access(os.path.dirname(here), os.W_OK):
        target = os.path.join(os.getcwd(), "boterdrop-solver")
    if os.path.exists(target):
        return None  # exists but unusable (checked by caller) — don't fight it
    print(f"(no solver checkout found — cloning {SOLVER_REPO} to {target}…)",
          flush=True)
    try:
        r = subprocess.run(["git", "clone", "--depth", "1", SOLVER_REPO, target],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=180)
        if r.returncode == 0 and os.path.isfile(os.path.join(target, "api_server.py")):
            print("(clone ok)", flush=True)
            return target
        print("(clone failed: " + r.stdout.decode('utf-8', 'replace')[-300:] + ")",
              flush=True)
    except Exception as e:
        print(f"(clone failed: {e} — set SOLVER_AUTOCLONE=0 to skip)", flush=True)
    return None


def solver_port():
    try:
        return urllib.parse.urlparse(SOLVER_BASE).port or 80
    except Exception:
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
    """Install the solver's python deps into THIS interpreter before launch.

    Needed because api_server.py imports fastapi/loguru/camoufox at module top,
    so its own _auto_install() never runs when those are missing (it would
    crash on line 7 first). Returns (ok, note). Set SOLVER_SKIP_DEPS=1 to
    skip (e.g. you manage deps in a venv yourself)."""
    if os.environ.get("SOLVER_SKIP_DEPS", "").lower() in ("1", "true", "yes"):
        return True, "dep check skipped"
    _ld = os.environ.get("LD_PRELOAD", "")
    if not _ld:
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
        for extra in ([], ["--break-system-packages"]):  # PEP 668 distros need the 2nd
            try:
                subprocess.run([sys.executable, "-m", "pip", "install", pkg] + extra,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=timeout, check=True, env=env)
                ok = True
                break
            except Exception:
                continue
        if not ok:
            return False, (f"pip install {pkg} failed — try manually in a venv: "
                           f"python3 -m venv venv && source venv/bin/activate && "
                           f"pip install {' '.join(SOLVER_DEPS)}")
        print(f"(installed {pkg})", flush=True)
    return True, "deps installed"


def _nix_lib_path():
    """LD_LIBRARY_PATH for the nix Firefox system libs (gtk3/nss closure).

    Discovery needs NO nix CLI (absent in deploys): it reads the profile
    manifest (~/.nix-profile/manifest.json) that Replit materializes from
    .replit [nix] packages, falling back to `nix profile list` / nix-store.
    gcc runtimes are excluded — they crash the Playwright node driver with
    'stack smashing' (the 'Connection closed' failure). Cached to
    /tmp/nixlibs.env. Returns "" when nothing is found (caller proceeds;
    the browser simply needs system libs present another way)."""
    cache = "/tmp/nixlibs.env"
    try:
        if os.path.isfile(cache):
            raw = open(cache).read().strip().rstrip(":")
            dirs = [d for d in raw.split(":") if d and os.path.isdir(d)]
            if len(dirs) > 10:
                return ":".join(dirs) + ":"
    except Exception:
        pass
    roots, seen = [], set()

    def _take(paths):
        for p in paths:
            if p not in seen and os.path.isdir(p):
                seen.add(p)
                roots.append(p)

    try:  # 1. profile manifest — works with zero nix tooling
        import json as _json
        for cand in (os.path.expanduser("~/.nix-profile/manifest.json"),
                     "/nix/var/nix/profiles/default/manifest.json"):
            try:
                with open(cand) as f:
                    man = _json.load(f)
                els = man.get("elements", {})
                vals = els.values() if isinstance(els, dict) else els
                for el in vals:
                    if not isinstance(el, dict) or not el.get("active", True):
                        continue
                    _take(el.get("storePaths", []) or [])
                if roots:
                    break
            except Exception:
                continue
    except Exception:
        pass
    if not roots:
        try:  # 2. nix CLI
            r = subprocess.run(["nix", "profile", "list"],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               timeout=60, text=True)
            _take(p for p in re.findall(r"/nix/store/[a-z0-9]+-[A-Za-z0-9+._-]+",
                                        r.stdout)
                  if not re.search(r"-(man|doc|dev|debug|bin)$", p))
        except Exception:
            pass
    if not roots:  # 3. gtk3's own runtime closure (has glib/dbus out-libs)
        try:
            g = subprocess.run(
                ["nix-store", "-qR",
                 next(p for p in roots if re.search(r"gtk\+3-", p))],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=120, text=True)
            _take(p for p in g.stdout.split()
                  if re.search(r"/(glib-2[\d.]+|dbus-1[\d.]+)(-lib)?$", p))
        except Exception:
            pass
    out = []
    for base in roots:
        lib = os.path.join(base, "lib")
        if not os.path.isdir(lib) or lib in out:
            continue
        try:
            names = os.listdir(lib)
        except Exception:
            continue
        if any(n.startswith(("libstdc++.so", "libgcc_s.so")) for n in names):
            continue  # poison for the node driver — skip
        out.append(lib)
    raw = ":".join(out) + (":" if out else "")
    try:
        if out:
            open(cache, "w").write(raw)
    except Exception:
        pass
    return raw


_xvfb_proc = None


def _ensure_xvfb():
    """Start our own Xvfb when the solver runs headed without a display.

    Finds Xvfb on PATH or in the nix profile (no xvfb-run script needed).
    Returns True when a display is available afterwards. Never raises."""
    global _xvfb_proc
    if os.environ.get("DISPLAY"):
        return True
    xbin = shutil.which("Xvfb")
    if not xbin:
        for cand in (os.path.expanduser("~/.nix-profile/bin/Xvfb"),
                     "/nix/var/nix/profiles/default/bin/Xvfb"):
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                xbin = cand
                break
    if not xbin:
        return False
    for disp in range(99, 110):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.bind(f"/tmp/.X11-unix/X{disp}")
        except Exception:
            continue  # display taken, try next
        try:
            _xvfb_proc = subprocess.Popen(
                [xbin, f":{disp}", "-screen", "0", "1280x720x24"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)
            time.sleep(1.5)
            if _xvfb_proc.poll() is None:
                os.environ["DISPLAY"] = f":{disp}"
                print(f"(Xvfb started on :{disp} for headed browser)", flush=True)
                return True
        except Exception:
            continue
    return False


def _launch_solver(sdir, port):
    """Start api_server.py in the background and wait until /health is ok."""
    global _solver_proc
    log_path = os.path.join(sdir, "solver_autostart.log")
    ok, note = _ensure_solver_deps()
    if not ok:
        print(f"(auto-start failed: {note})", flush=True)
        return False
    # The checkout's config may point at another port — align it with the
    # port we are about to poll so solver and chat agree.
    headed = True
    try:
        with open(os.path.join(sdir, "config.json")) as f:
            cfg = json.load(f)
        if cfg.get("port") != port:
            cfg["port"] = port
            with open(os.path.join(sdir, "config.json"), "w") as f:
                json.dump(cfg, f, indent=4)
            print(f"(solver config port set to {port})", flush=True)
        headed = not cfg.get("headless", True)
    except Exception as e:
        print(f"(could not read solver config: {e})", flush=True)
    nixlibs = _nix_lib_path()
    if nixlibs:
        os.environ["LD_LIBRARY_PATH"] = nixlibs + os.environ.get("LD_LIBRARY_PATH", "")
        print("(nix system libs wired for the browser)", flush=True)
    # Fix pyexpat undefined symbol by setting LD_PRELOAD
    os.environ["LD_PRELOAD"] = "/nix/store/sr4cnxyzx24ylxygfk7d81hy4791l8gm-expat-2.7.3/lib/libexpat.so.1"
    if headed and not _ensure_xvfb():
        print("(headed solver but no display/Xvfb — browser will likely fail; "
              "add xorg.xvfb to .replit [nix] packages)", flush=True)
    print(f"(starting solver from {sdir} — first run may download the browser; "
          f"log: {log_path})", flush=True)
    try:
        logf = open(log_path, "ab")
        env = os.environ.copy()
        env["LD_PRELOAD"] = "/nix/store/sr4cnxyzx24ylxygfk7d81hy4791l8gm-expat-2.7.3/lib/libexpat.so.1"
        proc = subprocess.Popen(
            [sys.executable, "api_server.py"], cwd=sdir,
            stdin=subprocess.PIPE, stdout=logf, stderr=subprocess.STDOUT,
            start_new_session=True, env=env)  # survive Ctrl-C in this chat
    except Exception as e:
        print(f"(auto-start failed to launch: {e})", flush=True)
        return False
    try:
        # answers the solver's "Lanjutkan? [Enter/Y]" prompt non-interactively
        proc.stdin.write(b"\n")
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
                      f"it keeps running after chat exits)", flush=True)
                return True
        except Exception:
            pass
        time.sleep(3)
    print(f"(auto-start timed out after {SOLVER_START_TIMEOUT}s — continuing "
          f"without solver)", flush=True)
    print(_log_tail(log_path), flush=True)
    hint = _hint_for_log(_log_tail(log_path))
    if hint:
        print(f"(fix: {hint})", flush=True)
    return False


def ensure_solver(autostart=True):
    """Solver-FIRST startup: guarantee a usable solver before chatting.

    1. healthy already? use it. 2. else auto-launch a local checkout
    (cloning it first if none exists). Records the outcome in _solver_note
    so later prompts can explain it. Returns True when solving will work;
    False -> manual recovery later. Never raises."""
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
        sdir = _clone_solver()  # last resort: fetch the checkout ourselves
    port = solver_port()
    if not sdir:
        if not os.environ.get("SOLVER_DIR", "").strip():
            _solver_note = "no checkout found and clone failed — start it manually"
            print("(auto-start skipped: no boterdrop-solver/api_server.py found — "
                  "set SOLVER_DIR or start it manually)", flush=True)
        else:
            _solver_note = "SOLVER_DIR checkout unusable"
        return False
    if port != 8001:
        _solver_note = f"SOLVER_BASE port is {port}, local checkout serves :8001"
        print(f"(auto-start skipped: {_solver_note} — run the solver manually)",
              flush=True)
        return False
    with socket.socket() as s:
        s.settimeout(2)
        if s.connect_ex(("127.0.0.1", port)) == 0:
            _solver_note = f"port {port} busy but not a solver"
            print(f"(auto-start skipped: {_solver_note} — check what's listening "
                  f"there)", flush=True)
            return False
    ok = _launch_solver(sdir, port)
    _solver_note = f"auto-started at {SOLVER_BASE}" if ok else \
        "auto-start failed — see messages above"
    return ok


def try_auto_solve():
    """Ask the solver for a token. Returns token str, or None (with a printed
    reason) when no solver is configured, none is running, or solving failed."""
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


class OxError(Exception):
    def __init__(self, message, code="upstream_error", status=502):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


class OxSession:
    """One upstream web session: cookies + CSRF, with refresh on demand."""

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

    def generate(self, messages, turnstile_token=None, timeout=120):
        """POST /api/chat. Yields ("text", str) / ("usage", dict) events.

        Raises OxError on turnstile / overload / bad request. Refreshes the
        session once transparently on HTTP 419 (CSRF mismatch)."""
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
            if e.code == 419:  # CSRF rotated -> refresh once and retry
                self.init()
                headers["X-CSRF-TOKEN"] = self.csrf
                try:
                    resp = _post(headers)
                except urllib.error.HTTPError as e2:
                    raise self._http_to_err(e2) from e2
            else:
                raise self._http_to_err(e) from e

        ctype = resp.headers.get("Content-Type", "")
        if "application/json" in ctype:  # e.g. 200 + turnstile_required JSON
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
        if status == 422 or "Invalid model" in msg:
            return OxError(msg, "invalid_request", 400)
        return OxError(msg, code or "upstream_error", status)

    @staticmethod
    def _parse_line(line):
        """One SSE line -> ("text", s) | ("usage", d) | ("error", msg) | None."""
        if line.startswith(":"):  # : OPENROUTER PROCESSING keep-alive
            return None
        if not line.startswith("data:"):
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
    """Run one generation with overload auto-retry. Returns (text, usage).

    Turnstile challenges are NOT auto-retried here — the caller decides
    (interactive prompt in REPL, clean error in --once mode)."""
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
                yield ("retry", f"upstream busy ({e.message[:100]}), retrying in {wait}s…")
                time.sleep(wait)
                continue
            yield ("failed", e)
            return


def turnstile_prompt(auto_tried):
    print("\n--- upstream asked for human verification (Cloudflare Turnstile) ---")
    if auto_tried:
        print("  (auto-solve didn't clear it — the token may have expired or the")
        print("   solver's IP may be limited too.)")
    elif _solver_note:
        print(f"  (solver status: {_solver_note})")
    print("  1. Open https://oxalpha.com/chat in your browser, solve the check,")
    print("     come back and press Enter to retry with a fresh session; or")
    print("  2. Paste an X-Turnstile-Token value after 't ' to retry with it; or")
    print("  3. Type q to give up on this message.")
    print("  (tip: chat auto-starts the solver on first launch and solves "
          "later challenges the same way; --no-solver disables it.)")
    try:
        ans = input("[Enter = retry | t <token> | q] ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if ans.lower().startswith("t ") and len(ans) > 2:
        return ("token", ans[2:].strip())
    if ans.lower() in ("q", "quit"):
        return None
    return ("retry", None)


def run_repl(sess, proxy=None):
    history, system = [], None
    print("Ox Alpha terminal chat — /help for commands, Ctrl-D to quit.")
    while True:
        try:
            user = input("\n> ").strip()
        except EOFError:
            print("\nbye.")
            return
        except KeyboardInterrupt:
            print("\n(interrupted — type /quit to exit)")
            continue
        if not user:
            continue
        if user.startswith("/"):
            cmd, _, arg = user[1:].partition(" ")
            if cmd in ("quit", "exit", "q"):
                print("bye.")
                return
            if cmd == "help":
                print("/new reset context | /retry resend last | /system <t> set system prompt")
                print("/save <file> dump transcript | /quit exit")
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
                print(f"(system prompt {'set' if system else 'cleared'})")
                continue
            elif cmd == "save":
                path = arg.strip() or "chat_transcript.md"
                with open(path, "w") as f:
                    for m in history:
                        f.write(f"**{m['role']}:** {m['content']}\n\n")
                print(f"(saved {len(history)} messages to {path})")
                continue
            else:
                print("(unknown command, /help)")
                continue
            # falls through for /retry with `user` set
        msgs = ([{"role": "system", "content": system}] if system else []) + history + [
            {"role": "user", "content": user}]
        history.append({"role": "user", "content": user})
        assistant = stream_with_recovery(sess, msgs, proxy)
        if assistant is None:  # user gave up on turnstile
            history.pop()
        elif assistant:
            history.append({"role": "assistant", "content": assistant})


def stream_with_recovery(sess, msgs, proxy):
    """Stream one reply, handling overload + turnstile. Returns text/None."""
    t_token, attempt, auto_tried = None, 0, False
    while True:
        attempt += 1
        gen = ask_generation_proxy(proxy, msgs) if proxy else ask_generation(sess, msgs, t_token)
        text_parts, failed = [], None
        try:
            for kind, val in gen:
                if kind == "text":
                    text_parts.append(val)
                    print(val, end="", flush=True)
                elif kind == "retry":
                    print(f"\n({val})", flush=True)
                elif kind == "failed":
                    failed = val
                    break
                elif kind == "done":
                    usage = val.get("usage", {}) or {}
                    print()
                    if usage.get("total_tokens"):
                        print(f"[tokens in={usage.get('prompt_tokens',0)} "
                              f"out={usage.get('completion_tokens',0)}]", flush=True)
                    return val["text"]
        except KeyboardInterrupt:
            print("\n(cancelled)")
            return "".join(text_parts) or None
        if failed is None:
            return "".join(text_parts)
        if failed.code == "turnstile_required":
            # refresh session (new cookies/CSRF, challenge may have cleared),
            # then recover — message is kept, nothing is lost.
            try:
                if not proxy:
                    sess.init()
            except Exception:
                pass
            if attempt > 3:
                print("\n(still challenged after 3 tries — try again later)")
                return None
            # First challenge for this message + solver configured (+ direct
            # mode, where we control the retry headers): solve automatically.
            if not proxy and not auto_tried and SOLVER_BASE:
                auto_tried = True
                t_token = try_auto_solve()
                if t_token:
                    continue  # retry same message with the solved token
            decision = turnstile_prompt(auto_tried=t_token is not None)
            if decision is None:
                print("(dropped that message — /retry to try again later)")
                return None
            if decision[0] == "token":
                t_token = decision[1]
            continue  # retry same message
        print(f"\n(error: {failed.message})")
        return "".join(text_parts) or ""


def ask_generation_proxy(proxy_base, messages):
    """Same event protocol as ask_generation, but via the OpenAI proxy."""
    import urllib.error
    body = json.dumps({"model": "ox-alpha", "messages": messages, "stream": True}).encode()
    req = urllib.request.Request(proxy_base.rstrip("/") + "/chat/completions",
                                 data=body, headers={"Content-Type": "application/json"})
    try:
        resp = urllib.request.urlopen(req, timeout=180)
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode() or "{}").get("error", {})
            msg = err.get("message", f"proxy HTTP {e.code}")
            code = err.get("code", "")
        except Exception:
            msg, code = f"proxy HTTP {e.code}", ""
        if e.code == 429 and "turnstile" in code:
            yield ("failed", OxError(msg, "turnstile_required", 429))
        elif e.code == 429:
            yield ("failed", OxError(msg, "overloaded", 429))
        else:
            yield ("failed", OxError(msg, "upstream_error", e.code))
        return
    except Exception as e:
        yield ("failed", OxError(f"cannot reach proxy: {e}", "proxy_down"))
        return
    parts = []
    try:
        buf = b""
        while True:
            chunk = resp.read(4096)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                s = line.decode("utf-8", "replace").strip()
                if not s.startswith("data:"):
                    continue
                data = s[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    delta = json.loads(data)["choices"][0]["delta"]
                except Exception:
                    continue
                if delta.get("content"):
                    parts.append(delta["content"])
                    yield ("text", delta["content"])
        yield ("done", {"text": "".join(parts), "usage": {}})
    finally:
        try:
            resp.close()
        except Exception:
            pass


def once(sess, prompt, proxy, system=None):
    msgs = ([{"role": "system", "content": system}] if system else []) + [
        {"role": "user", "content": prompt}]
    if proxy:
        gen = ask_generation_proxy(proxy, msgs)
        for kind, val in gen:
            if kind == "text":
                print(val, end="", flush=True)
            elif kind == "failed":
                print(f"error: {val.message}", file=sys.stderr)
                return 1
            elif kind == "done":
                print()
                return 0
        return 0
    text = stream_with_recovery(sess, msgs, None)
    return 0 if text else 1


def main():
    global BASE, SOLVER_BASE, SOLVER_TIMEOUT, SOLVER_AUTOSTART
    ap = argparse.ArgumentParser(description="Ox Alpha terminal chat")
    ap.add_argument("--once", metavar="MSG", help="send one message, print reply, exit")
    ap.add_argument("--proxy", metavar="URL", default=None,
                    help="route via oxalpha-openai-proxy, e.g. http://127.0.0.1:8000/v1")
    ap.add_argument("--solver", metavar="URL", default=None,
                    help="Boterdrop-Solver URL for Turnstile auto-solve "
                         "(default: http://127.0.0.1:8001, or SOLVER_BASE env)")
    ap.add_argument("--no-solver", action="store_true",
                    help="disable Turnstile auto-solve, always ask manually")
    ap.add_argument("--no-autostart", action="store_true",
                    help="don't auto-launch a local solver, only use one "
                         "already running (or SOLVER_AUTOSTART=0)")
    ap.add_argument("--system", default=None, help="system prompt")
    ap.add_argument("--base", default=BASE, help="upstream base (debugging)")
    args = ap.parse_args()
    BASE = args.base.rstrip("/")
    if args.no_solver:
        SOLVER_BASE = ""
    elif args.solver:
        SOLVER_BASE = args.solver.rstrip("/")
    if args.no_autostart:
        SOLVER_AUTOSTART = False

    sess = None
    if not args.proxy:
        sess = OxSession()
        try:
            sess.init()
        except OxError as e:
            print(f"could not start upstream session: {e.message}", file=sys.stderr)
            return 1
        print(f"(session ready, model {UPSTREAM_MODEL})")
        if SOLVER_BASE and not args.proxy:
            print(f"(Turnstile auto-solve: {SOLVER_BASE})")
            # Solver FIRST: boot it now so the first challenge solves instead
            # of failing; later challenges reuse it on demand.
            ensure_solver()
    else:
        print(f"(proxy mode: {args.proxy})")
        if SOLVER_BASE:
            print("(note: --solver only applies to direct mode; in proxy mode set "
                  "SOLVER_BASE on the server instead)")

    if args.once:
        if args.proxy:
            return once(None, args.once, args.proxy, args.system)
        msgs = ([{"role": "system", "content": args.system}] if args.system else []) + [
            {"role": "user", "content": args.once}]
        # non-interactive: overload auto-retries inside ask_generation; for a
        # Turnstile challenge, try the solver once (if configured), else fail
        # with a hint — no interactive prompts in --once mode.
        fail, solver_tried = None, False
        gen_token = None
        while True:
            for kind, val in ask_generation(sess, msgs, gen_token):
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
            if fail.code == "turnstile_required" and SOLVER_BASE and not solver_tried:
                solver_tried = True
                try:
                    sess.init()
                except Exception:
                    pass
                gen_token = try_auto_solve()
                if gen_token:
                    fail = None
                    continue  # retry once with the solved token
            break
        print(f"error [{fail.code}]: {fail.message}", file=sys.stderr)
        if fail.code == "turnstile_required":
            print("hint: open https://oxalpha.com/chat in a browser to clear the "
                  "check, then retry — or rerun in REPL mode for guided recovery.",
                  file=sys.stderr)
        return 1
    run_repl(sess, args.proxy)
    return 0


if __name__ == "__main__":
    sys.exit(main())
