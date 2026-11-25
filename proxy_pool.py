#!/usr/bin/env python3
"""
proxy_pool.py — free-proxy scraper + checker + rotating pool for OxAlpha.

Why: oxalpha.com (free demo) enforces per-IP daily message caps. During
testing/development you can exhaust your own IP's quota; rotating through
public HTTP proxies gives each upstream call a fresh IP. This module:

  1. SCRAPES  plain `ip:port` lists from public keyless sources
  2. CHECKS   them in two stages:
       stage 1 — HTTPS-over-proxy CONNECT to a lightweight judge (api.ipify.org)
       stage 2 — for survivors: an actual GET https://oxalpha.com/chat through
                 the proxy (capped sample, so we don't hammer the upstream)
  3. POOLS    the verified proxies in a JSON file the server hot-reloads;
              the server rotates on every upstream call and cools down proxies
              that get rate-limited (429) or fail to connect.

Use as a library (`ProxyPool`) or CLI:

  python3 proxy_pool.py --scrape --check --limit 300 --concurrency 64 \
      --stage2-sample 20 --out proxies.json
  python3 proxy_pool.py --check-existing --out proxies.json
  python3 proxy_pool.py --stats --out proxies.json

Env (server side): PROXY_POOL=1 PROXY_POOL_FILE=proxies.json

Be a good citizen: modest concurrency, short timeouts, and the checker touches
oxalpha.com at most `stage2-sample` times per run.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Iterable

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

IP_PORT_RE = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3}):(\d{2,5})$")

DEFAULT_SOURCES = [
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=6000&country=all",
    "https://www.proxy-list.download/api/v1/get?type=http",
    "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt",
]

STAGE1_JUDGE = "https://api.ipify.org/?format=json"   # tiny, https → tests CONNECT
STAGE2_TARGET = "https://oxalpha.com/chat"           # the host we actually need


# ------------------------------------------------------------- scraper -------

def scrape(sources: Iterable[str] = DEFAULT_SOURCES, limit: int = 0,
           timeout: float = 15.0) -> list[str]:
    """Fetch + dedupe + validate `ip:port` lines from public list sources."""
    found: dict[str, str] = {}
    for src in sources:
        try:
            req = urllib.request.Request(src, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                text = r.read().decode("utf-8", "replace")
        except Exception as e:
            print(f"  [scrape] {src[:60]}… -> FAILED ({e})", file=sys.stderr)
            continue
        n0 = len(found)
        for line in text.splitlines():
            line = line.strip()
            m = IP_PORT_RE.match(line)
            if not m:
                continue
            ip, port = m.group(1), m.group(2)
            # skip obvious junk ports and non-global IPs
            if int(port) > 65535 or ip.startswith(("0.", "10.", "127.", "192.168.",
                                                   "172.16.", "172.17.", "172.18.",
                                                   "172.19.", "172.2", "172.30.", "172.31.")):
                continue
            found.setdefault(f"{ip}:{port}", src[:40])
        print(f"  [scrape] {src[:60]}… -> {len(found) - n0} new "
              f"(total {len(found)})", file=sys.stderr)
        if limit and len(found) >= limit:
            break
    proxies = list(found)[:limit] if limit else list(found)
    return proxies


# ------------------------------------------------------------- checker -------

def _opener(proxy: str | None) -> urllib.request.OpenerDirector:
    if proxy:
        handler = urllib.request.ProxyHandler(
            {"http": f"http://{proxy}", "https": f"http://{proxy}"})
    else:
        handler = urllib.request.ProxyHandler({})  # force direct (ignore env)
    return urllib.request.build_opener(handler)


def _stage1(proxy: str, timeout: float = 6.0) -> tuple[bool, float]:
    """HTTPS CONNECT through the proxy to the judge. Returns (ok, latency_s)."""
    t0 = time.time()
    req = urllib.request.Request(STAGE1_JUDGE, headers={"User-Agent": UA})
    try:
        with _opener(proxy).open(req, timeout=timeout) as r:
            r.read(256)
        return True, time.time() - t0
    except Exception:
        return False, time.time() - t0


def _stage2(proxy: str, timeout: float = 12.0) -> bool:
    """Can this proxy actually reach the upstream host?"""
    req = urllib.request.Request(STAGE2_TARGET, headers={"User-Agent": UA})
    try:
        with _opener(proxy).open(req, timeout=timeout) as r:
            r.read(512)
            return r.status == 200
    except Exception:
        return False


def check(proxies: list[str], concurrency: int = 64, stage2_sample: int = 20,
          progress: bool = True) -> list[dict]:
    """Two-stage check. Returns records for stage-1 survivors (state set
    according to stage 2). Stage 2 runs on at most `stage2_sample` survivors."""
    survivors: list[tuple[str, float]] = []
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
        futs = {ex.submit(_stage1, p): p for p in proxies}
        done = 0
        for fut in as_completed(futs):
            done += 1
            p = futs[fut]
            ok, lat = fut.result()
            if ok:
                survivors.append((p, lat))
            if progress and done % 25 == 0:
                print(f"  [check] {done}/{len(proxies)} tested, "
                      f"{len(survivors)} alive", file=sys.stderr)
    survivors.sort(key=lambda x: x[1])  # fastest first
    print(f"  [check] stage 1: {len(survivors)}/{len(proxies)} alive "
          f"({100 * len(survivors) / max(1, len(proxies)):.1f}%)", file=sys.stderr)

    sample = [p for p, _ in survivors[:stage2_sample]]
    stage2_ok: set[str] = set()
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(sample)))) as ex:
        futs = {ex.submit(_stage2, p): p for p in sample}
        for fut in as_completed(futs):
            if fut.result():
                stage2_ok.add(futs[fut])
    print(f"  [check] stage 2: {len(stage2_ok)}/{len(sample)} reach "
          f"oxalpha.com", file=sys.stderr)

    lat_by_proxy = dict(survivors)
    records: list[dict] = []
    for p, _lat in survivors:
        state = "ok" if p in stage2_ok else "unverified"
        records.append({"proxy": p, "state": state, "ok": 0, "fail": 0,
                        "consec_fail": 0, "latency_ms": int(lat_by_proxy[p] * 1000),
                        "last_ok": 0.0, "last_fail": 0.0, "cooldown_until": 0.0})
    return records


# --------------------------------------------------------------- pool --------

class ProxyPool:
    """JSON-file-backed rotating pool with hot reload + cooldowns."""

    def __init__(self, path: str = "proxies.json", enabled: bool = False):
        self.path = path
        self.enabled = enabled
        self._lock = threading.Lock()
        self._records: dict[str, dict] = {}
        self._rr = 0
        self._mtime = 0.0

    # -- persistence --
    def load(self) -> None:
        with self._lock:
            if not os.path.isfile(self.path):
                self._records = {}
                return
            try:
                with open(self.path) as f:
                    data = json.load(f)
                self._records = {r["proxy"]: r for r in data.get("proxies", [])
                                 if isinstance(r, dict) and r.get("proxy")}
                self._mtime = os.path.getmtime(self.path)
            except Exception:
                self._records = {}

    def save(self) -> None:
        with self._lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"updated": datetime.now(timezone.utc).isoformat(),
                           "proxies": sorted(self._records.values(),
                                             key=lambda r: (-r.get("ok", 0),
                                                            r.get("latency_ms", 0)))},
                          f, indent=1)
            os.replace(tmp, self.path)
            self._mtime = os.path.getmtime(self.path) if os.path.isfile(self.path) else 0.0

    def _hot_reload(self) -> None:
        try:
            mtime = os.path.getmtime(self.path) if os.path.isfile(self.path) else 0.0
            if mtime != self._mtime:
                self.load()
        except Exception:
            pass

    # -- rotation --
    def pick(self, exclude: set[str] | None = None, now: float | None = None) -> str | None:
        """Round-robin over usable proxies (state ok/unverified, not cooling
        down, not excluded). Returns None when the pool can't provide one."""
        if not self.enabled:
            return None
        self._hot_reload()
        now = now if now is not None else time.time()
        exclude = exclude or set()
        with self._lock:
            usable = [r for r in self._records.values()
                      if r.get("state") in ("ok", "unverified")
                      and r.get("cooldown_until", 0) <= now
                      and r["proxy"] not in exclude]
            if not usable:
                return None
            self._rr += 1
            return usable[self._rr % len(usable)]["proxy"]

    # -- feedback --
    def report_ok(self, proxy: str | None, latency_ms: int = 0) -> None:
        if not proxy:
            return
        self._hot_reload()
        with self._lock:
            r = self._records.setdefault(proxy, self._blank(proxy))
            r["state"] = "ok"
            r["ok"] = r.get("ok", 0) + 1
            r["consec_fail"] = 0
            r["last_ok"] = time.time()
            r["cooldown_until"] = 0.0
            if latency_ms:
                r["latency_ms"] = latency_ms

    def report_fail(self, proxy: str | None, hard: bool = False) -> None:
        """hard=True: connection-level failure. hard=False: proxy works but is
        upstream-rate-limited (429) — long cooldown instead of eviction."""
        if not proxy:
            return
        self._hot_reload()
        with self._lock:
            r = self._records.setdefault(proxy, self._blank(proxy))
            r["fail"] = r.get("fail", 0) + 1
            r["consec_fail"] = r.get("consec_fail", 0) + 1
            r["last_fail"] = time.time()
            if hard:
                r["cooldown_until"] = time.time() + 30
                if r["consec_fail"] >= 3:
                    r["state"] = "down"
            else:
                r["cooldown_until"] = time.time() + 600

    @staticmethod
    def _blank(proxy: str) -> dict:
        return {"proxy": proxy, "state": "unverified", "ok": 0, "fail": 0,
                "consec_fail": 0, "latency_ms": 0, "last_ok": 0.0,
                "last_fail": 0.0, "cooldown_until": 0.0}

    def stats(self) -> dict:
        self._hot_reload()
        now = time.time()
        with self._lock:
            by_state: dict[str, int] = {}
            avail = 0
            for r in self._records.values():
                by_state[r.get("state", "?")] = by_state.get(r.get("state", "?"), 0) + 1
                if r.get("state") in ("ok", "unverified") and \
                        r.get("cooldown_until", 0) <= now:
                    avail += 1
        return {"enabled": self.enabled, "file": self.path,
                "total": len(self._records), "available_now": avail,
                "by_state": by_state}

    def build_records(self, checked: list[dict]) -> None:
        with self._lock:
            for rec in checked:
                old = self._records.get(rec["proxy"], {})
                merged = self._blank(rec["proxy"])
                merged.update({k: old[k] for k in ("ok", "fail", "consec_fail",
                                                   "last_ok", "last_fail")
                               if k in old})
                merged.update({k: rec[k] for k in ("state", "latency_ms")})
                self._records[rec["proxy"]] = merged


# ---------------------------------------------------------------- CLI --------

def main() -> None:
    ap = argparse.ArgumentParser(description="free-proxy scraper/checker/pool for OxAlpha")
    ap.add_argument("--scrape", action="store_true", help="fetch public lists")
    ap.add_argument("--check", action="store_true", help="check scraped proxies (needs --scrape or --check-existing)")
    ap.add_argument("--check-existing", action="store_true", help="re-check proxies already in the pool file")
    ap.add_argument("--limit", type=int, default=300, help="max proxies to scrape (default 300)")
    ap.add_argument("--concurrency", type=int, default=64, help="stage-1 threads (default 64)")
    ap.add_argument("--stage2-sample", type=int, default=20, help="max proxies to test against oxalpha (default 20)")
    ap.add_argument("--out", default="proxies.json", help="pool file (default proxies.json)")
    args = ap.parse_args()

    pool = ProxyPool(args.out, enabled=True)

    if args.check_existing:
        pool.load()
        candidates = [r["proxy"] for r in pool._records.values()
                      if r.get("state") in ("ok", "unverified", "down")]
        print(f"[pool] re-checking {len(candidates)} existing proxies", file=sys.stderr)
        if candidates and args.check:
            pool.build_records(check(candidates, args.concurrency, args.stage2_sample))
            pool.save()
            print(f"[pool] saved -> {args.out}: {pool.stats()}", file=sys.stderr)
        return

    if not args.scrape:
        print("[pool] nothing to do (use --scrape --check)", file=sys.stderr)
        print(f"[pool] stats: {pool.stats()}", file=sys.stderr)
        return

    print("[scrape] fetching public proxy lists…", file=sys.stderr)
    proxies = scrape(limit=args.limit)
    print(f"[scrape] {len(proxies)} unique proxies collected", file=sys.stderr)
    if args.check and proxies:
        checked = check(proxies, args.concurrency, args.stage2_sample)
        pool.build_records(checked)
        pool.save()
        st = pool.stats()
        print(f"[pool] saved -> {args.out}: {st}", file=sys.stderr)
        ok = [r["proxy"] for r in pool._records.values() if r.get("state") == "ok"]
        if ok:
            print(f"[pool] proxies that reach oxalpha.com "
                  f"(fastest first): {ok[:10]}", file=sys.stderr)
        else:
            print("[pool] none reached oxalpha.com this run — try again later "
                  "or raise --limit/--stage2-sample", file=sys.stderr)
    else:
        with open("proxies_full.txt", "w") as f:
            f.write("\n".join(proxies))
        print(f"[scrape] wrote raw list -> proxies_full.txt "
              f"(run with --check to verify)", file=sys.stderr)


if __name__ == "__main__":
    main()
