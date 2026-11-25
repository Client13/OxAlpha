"""Unit tests for the issue-#1 robustness fixes in api_server.py.

Runs WITHOUT a real browser: stub out fastapi/loguru/camoufox/uvicorn/playwright
before import, then exercise the pure helpers with fakes.

  python3 test_robustness.py
"""
import asyncio
import sys
import types

# ---- stub heavy third-party modules ---------------------------------------
fastapi_mod = types.ModuleType("fastapi")
responses_mod = types.ModuleType("fastapi.responses")


class _FastAPI:
    def __init__(self, *a, **k):
        pass

    def add_event_handler(self, *a, **k):
        pass

    def get(self, *a, **k):
        def deco(fn):
            return fn
        return deco

    def post(self, *a, **k):
        def deco(fn):
            return fn
        return deco


def _Query(*a, **k):
    return None


class _HTTPException(Exception):
    def __init__(self, status_code=500, detail=None):
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


class _Request:
    pass


class _JSONResponse:
    def __init__(self, content=None, status_code=200):
        self.content = content
        self.status_code = status_code


fastapi_mod.FastAPI = _FastAPI
fastapi_mod.Query = _Query
fastapi_mod.HTTPException = _HTTPException
fastapi_mod.Request = _Request
responses_mod.JSONResponse = _JSONResponse
fastapi_mod.responses = responses_mod
sys.modules["fastapi"] = fastapi_mod
sys.modules["fastapi.responses"] = responses_mod

loguru_mod = types.ModuleType("loguru")


class _Logger:
    def __getattr__(self, name):
        def _log(*a, **k):
            pass
        return _log


loguru_mod.logger = _Logger()
sys.modules["loguru"] = loguru_mod

camoufox_mod = types.ModuleType("camoufox")
camoufox_mod.DefaultAddons = types.SimpleNamespace(UBO="ubo")
async_api_mod = types.ModuleType("camoufox.async_api")
async_api_mod.AsyncCamoufox = object
sys.modules["camoufox"] = camoufox_mod
sys.modules["camoufox.async_api"] = async_api_mod
sys.modules["uvicorn"] = types.ModuleType("uvicorn")

import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import api_server  # noqa: E402

Server = api_server.ClearanceAPIServer
passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name}")
    else:
        failed += 1
        print(f"  FAIL {name}")


def make_server(**kw):
    s = Server.__new__(Server)
    # replicate __init__ state without FastAPI side effects beyond stubs
    s.__init__(
        headless=True,
        thread=kw.get("thread", 2),
        page_count=kw.get("page_count", 1),
        proxy_support=kw.get("proxy_support", False),
        proxy_file=kw.get("proxy_file", "proxies.txt"),
        cleanup_interval_minutes=10,
    )
    return s


async def main():
    global passed, failed
    print("== driver-dead detection ==")
    check("connection-closed detected",
          Server._is_driver_dead_error(Exception("BrowserContext.cookies: Connection closed while reading from the driver")))
    check("target-closed detected",
          Server._is_driver_dead_error(Exception("Page.route: Target closed")))
    check("plain error not flagged",
          not Server._is_driver_dead_error(Exception("cf_clearance tidak ditemukan")))
    check("timeout error not flagged",
          not Server._is_driver_dead_error(asyncio.TimeoutError()))

    print("== proxy server builder ==")
    check("auth proxy", Server._proxy_server("http://user:pass@1.2.3.4:8080") == "http://user:pass@1.2.3.4:8080" or
          Server._proxy_server("http://user:pass@1.2.3.4:8080") == "http://1.2.3.4:8080")
    check("no-port defaults 80", Server._proxy_server("http://1.2.3.4") == "http://1.2.3.4:80")
    check("https no-port defaults 443", Server._proxy_server("https://1.2.3.4") == "https://1.2.3.4:443")
    check("invalid -> None", Server._proxy_server("notaproxy") is None)

    print("== pool helpers ==")
    s = make_server()

    class FakePage:
        def __init__(self, alive=True):
            self.alive = alive
            self.closed = False

        async def unroute_all(self):
            pass

        async def close(self):
            self.closed = True

    class FakeCtx:
        def __init__(self, alive=True):
            self.alive = alive

        async def cookies(self):
            if not self.alive:
                raise Exception("Connection closed while reading from the driver")
            return []

    # healthy page is recycled
    p, c = FakePage(), FakeCtx()
    s.contexts = [c]
    check("healthy page recycled", await s._return_page(p, c) is True and s.page_pool.qsize() == 1)

    # dead context page is discarded, pool not poisoned
    p2, c2 = FakePage(), FakeCtx(alive=False)
    s.contexts = [c2]
    # drain pool first
    while not s.page_pool.empty():
        s.page_pool.get_nowait()
    check("dead page discarded", await s._return_page(p2, c2) is False and s.page_pool.empty()
          and p2.closed is True)

    # stale (post-restart) context page is discarded
    p3, c3 = FakePage(), FakeCtx()
    s.contexts = []  # restart wiped the list
    check("stale page discarded", await s._return_page(p3, c3) is False and p3.closed is True)

    # _get_page timeout -> PoolExhausted instead of hanging forever
    try:
        await s._get_page(timeout=1)
        check("pool exhausted raises", False)
    except s._PoolExhausted:
        check("pool exhausted raises", True)

    print("== restart heals pool ==")

    class FakeBrowser:
        def __init__(self):
            self.closed = False

        def is_connected(self):
            return not self.closed

        async def close(self):
            self.closed = True

        async def new_context(self):
            return FakeCtx()

    class FakeCamoufox:
        async def start(self):
            return FakeBrowser()

    # extend FakeCtx with new_page for rebuild
    async def _new_page(self):
        return FakePage()
    FakeCtx.new_page = _new_page

    s2 = make_server(thread=1, page_count=2)
    s2.camoufox = FakeCamoufox()
    s2.browser = FakeBrowser()
    s2.contexts = [FakeCtx(alive=False)]  # poisoned generation
    await s2._restart_browser()
    check("restart rebuilds full pool", s2.page_pool.qsize() == 2)
    check("restart replaces contexts", len(s2.contexts) == 1)
    # every pooled page usable
    ok = True
    while not s2.page_pool.empty():
        pp, cc = s2.page_pool.get_nowait()
        try:
            await cc.cookies()
        except Exception:
            ok = False
    check("rebuilt pool pages alive", ok)

    print("== creat_app factory ==")
    app = api_server.create_app(headless=True, thread=1, page_count=1,
                                proxy_support=False, cleanup_interval_minutes=10)
    check("factory returns app", app is not None)

    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


asyncio.run(main())
