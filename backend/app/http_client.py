"""One pooled HTTP client per event loop (overhaul plan, phase 2).

`async with httpx.AsyncClient(...)` around every call opens a new TCP and
TLS connection each time: MEXC, Binance and GeckoTerminal are hit several
times per chart. `pooled()` hands out a shared client that keeps
connections alive, with the timeout applied per request.

Keyed by event loop because an httpx client cannot cross loops (tests and
`asyncio.run` in worker threads each have their own). Cookies are refused:
the client is shared by unrelated services. When a test has replaced
`httpx.AsyncClient`, a fresh client is built per call as before, so the
fakes keep working.
"""
from __future__ import annotations

import asyncio
import weakref
from contextlib import asynccontextmanager
from http.cookiejar import CookieJar, DefaultCookiePolicy

import httpx

_REAL_ASYNC_CLIENT = httpx.AsyncClient
_clients: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, httpx.AsyncClient]" = weakref.WeakKeyDictionary()
_LIMITS = httpx.Limits(max_connections=50, max_keepalive_connections=20, keepalive_expiry=30.0)


class _Bound:
    """The shared client with this caller's timeout on every request."""

    def __init__(self, client: httpx.AsyncClient, timeout: float) -> None:
        self._client = client
        self._timeout = timeout

    async def request(self, method: str, url, **kwargs) -> httpx.Response:
        kwargs.setdefault("timeout", self._timeout)
        return await self._client.request(method, url, **kwargs)

    async def get(self, url, **kwargs) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def post(self, url, **kwargs) -> httpx.Response:
        return await self.request("POST", url, **kwargs)


def _shared() -> httpx.AsyncClient:
    loop = asyncio.get_running_loop()
    client = _clients.get(loop)
    if client is None or client.is_closed:
        jar = CookieJar(policy=DefaultCookiePolicy(allowed_domains=[]))
        client = _REAL_ASYNC_CLIENT(limits=_LIMITS, cookies=httpx.Cookies(jar))
        _clients[loop] = client
    return client


@asynccontextmanager
async def pooled(timeout: float = 15.0):
    if httpx.AsyncClient is not _REAL_ASYNC_CLIENT:
        async with httpx.AsyncClient(timeout=timeout) as client:
            yield client
        return
    yield _Bound(_shared(), timeout)


async def aclose() -> None:
    """Closes this loop's client (app shutdown)."""
    client = _clients.pop(asyncio.get_running_loop(), None)
    if client is not None:
        await client.aclose()
