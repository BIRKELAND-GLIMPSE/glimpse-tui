"""One pooled httpx client per host, a descriptive User-Agent, and one SOCKS5 setting (Tor) for all of them.

Tests install a transport with `use_transport`, so nothing in the suite can reach the network: a request with no
fixture behind it fails loudly instead of going out.
"""
from __future__ import annotations

from urllib.parse import urlsplit

import httpx

from .. import __version__

USER_AGENT = f"glimpse-tui/{__version__} (+https://github.com/BIRKELAND-GLIMPSE/glimpse-tui)"
TIMEOUT = httpx.Timeout(12.0, connect=8.0)

_clients: dict[tuple[str, str], httpx.AsyncClient] = {}
_transport: httpx.AsyncBaseTransport | None = None
_socks5 = ""


def use_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    """Route every request through `transport` (tests). None restores the network."""
    global _transport
    _transport = transport
    _clients.clear()


def use_socks5(url: str) -> None:
    """`socks5h://127.0.0.1:9050` sends every request through Tor. Takes effect on the next request."""
    global _socks5
    if url != _socks5:
        _socks5 = url
        _clients.clear()


def socks5() -> str:
    return _socks5


def client(url: str, user_agent: str = "") -> httpx.AsyncClient:
    """The shared client for the host of `url`. SEC needs its own User-Agent, so that is part of the pool key."""
    host = urlsplit(url).netloc
    key = (host, user_agent)
    c = _clients.get(key)
    if c is None or c.is_closed:
        kw: dict = {"timeout": TIMEOUT, "headers": {"User-Agent": user_agent or USER_AGENT, "Accept-Encoding": "gzip"},
                    "follow_redirects": True, "limits": httpx.Limits(max_connections=4, max_keepalive_connections=2)}
        if _transport is not None:
            kw["transport"] = _transport
        elif _socks5:
            kw["proxy"] = _socks5           # needs httpx[socks]; a bad URL fails the request, never bypasses the proxy
        c = _clients[key] = httpx.AsyncClient(**kw)
    return c


async def close_all() -> None:
    for c in list(_clients.values()):
        try:
            await c.aclose()
        except Exception:
            pass
    _clients.clear()
