"""Bitcoin Core JSON-RPC, optional: `bitcoin.core_rpc = "http://127.0.0.1:8332"` (TERMINAL.md rule 4).

Authentication is the node's cookie file by default (`~/.bitcoin/.cookie`, read fresh for every request because the
node rewrites it on each start), or `user:pass` in the URL. Credentials are split off the URL at once: they are never
part of `base_url`, of an error message, of the health record or of a log line. Requests go through the shared
pooled client, so the SOCKS5 setting and the test transport apply here as everywhere.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

import httpx

from . import http
from .core import LIVE, Health, Provenance, SourceError


def default_cookie() -> Path:
    """Where Core keeps `.cookie` on this platform (mainnet)."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Bitcoin" / ".cookie"
    if sys.platform.startswith("win"):
        return Path(os.environ.get("APPDATA", str(Path.home()))) / "Bitcoin" / ".cookie"
    return Path.home() / ".bitcoin" / ".cookie"


def split_url(url: str) -> tuple[str, tuple[str, str] | None]:
    """(`http://host:8332` with no userinfo, (user, password) or None)."""
    u = urlsplit(url.strip())
    auth = (unquote(u.username or ""), unquote(u.password or "")) if u.username or u.password else None
    host = u.hostname or ""
    host = f"[{host}]" if ":" in host else host
    clean = urlunsplit((u.scheme, host + (f":{u.port}" if u.port else ""), u.path.rstrip("/"), "", ""))
    return (clean if u.scheme and host else ""), auth


class CoreRpc:
    """Every call returns `(result, Provenance)`. A node that is not configured refuses, and sends nothing."""

    serves = "tip, block hashes, blocks, raw transactions, mempool size, smart fee estimates (your own node)"

    def __init__(self, url: str = "", cookie: str | Path | None = None) -> None:
        self.base_url, self._auth = split_url(url)
        self.cookie = Path(cookie).expanduser() if cookie else None
        self.name, self.delay, self.health = "bitcoin core", LIVE, Health()
        self._id = 0

    def point_at(self, url: str) -> None:
        base, auth = split_url(url)
        if (base, auth) != (self.base_url, self._auth):
            self.base_url, self._auth, self.health = base, auth, Health()

    @property
    def host(self) -> str:
        return urlsplit(self.base_url).netloc

    def __repr__(self) -> str:                              # a stray log of the object shows the host and nothing else
        return f"CoreRpc({self.base_url!r})"

    def prov(self, at: float) -> Provenance:
        return Provenance(self.name, at, at, self.delay)

    async def _credentials(self) -> tuple[str, str]:
        if self._auth:
            return self._auth
        candidates = [self.cookie] if self.cookie else [Path.home() / ".bitcoin" / ".cookie", default_cookie()]
        for f in candidates:
            try:
                user, _, password = (await asyncio.to_thread(f.read_text)).strip().partition(":")
            except OSError:
                continue
            if user and password:
                return user, password
        raise SourceError(f"{self.name}: no cookie file found. Start bitcoind with server=1, or put user:pass in bitcoin.core_rpc")

    async def call(self, method: str, *params: Any) -> tuple[Any, Provenance]:
        if not self.base_url:
            raise SourceError(f"{self.name}: no node configured. SET bitcoin.core_rpc http://127.0.0.1:8332")
        try:
            auth = await self._credentials()
            self._id += 1
            body = {"jsonrpc": "1.0", "id": self._id, "method": method, "params": list(params)}
            t0 = time.monotonic()
            try:
                r = await http.client(self.base_url).post(self.base_url + "/", json=body, auth=auth)
            except httpx.HTTPError as e:
                raise SourceError(f"{self.name}: {type(e).__name__}") from None        # the type only: never the request
            self.health.latency_ms = (time.monotonic() - t0) * 1000
            if r.status_code in (401, 403):
                raise SourceError(f"{self.name}: HTTP {r.status_code}. Check the cookie file, or rpcauth and rpcallowip on the node")
            try:
                reply = r.json()
            except ValueError:
                raise SourceError(f"{self.name}: HTTP {r.status_code}") from None
            if not isinstance(reply, dict):
                raise SourceError(f"{self.name}: HTTP {r.status_code}")
            if err := reply.get("error"):                   # Core answers 404 or 500 with the reason in the body
                code, msg = (err.get("code"), err.get("message", "")) if isinstance(err, dict) else ("", err)
                raise SourceError(f"{self.name}: {method} failed ({code}): {str(msg)[:120]}")
            if r.status_code != 200:
                raise SourceError(f"{self.name}: HTTP {r.status_code}")
        except SourceError as e:
            self.health.errors += 1
            self.health.last_error, self.health.last_error_at = str(e), time.time()
            raise
        now = time.time()
        self.health.ok, self.health.last_ok_at = self.health.ok + 1, now
        return reply.get("result"), self.prov(now)

    # the RPCs the terminal uses ─────────────────────────────
    async def getblockchaininfo(self): return await self.call("getblockchaininfo")
    async def getblockhash(self, height: int): return await self.call("getblockhash", int(height))
    async def getmempoolinfo(self): return await self.call("getmempoolinfo")

    async def getblock(self, block_hash: str, verbosity: int = 1):
        """1: the header fields and txids. 2: every transaction decoded (tens of MB for a full block)."""
        return await self.call("getblock", block_hash, 2 if verbosity >= 2 else 1)

    async def getrawtransaction(self, txid: str, verbose: bool = True, block_hash: str | None = None):
        """Needs txindex=1 for a confirmed transaction, unless the block hash is given."""
        args = [txid, 1 if verbose else 0] + ([block_hash] if block_hash else [])
        return await self.call("getrawtransaction", *args)

    async def estimatesmartfee(self, target: int, mode: str = "economical"):
        return await self.call("estimatesmartfee", int(target), mode)

    # the same capability names the REST backends answer to ──

    async def tip_height(self) -> tuple[int, Provenance]:
        v, p = await self.getblockchaininfo()
        return int(v["blocks"]), p

    async def block_hash(self, height: int) -> tuple[str, Provenance]:
        v, p = await self.getblockhash(height)
        return str(v), p

    async def fees(self) -> tuple[dict[str, float], Provenance]:
        """The recommended-fees shape. `feerate` is BTC per kvB; a target with no estimate takes the next slower one."""
        rates: dict[str, float] = {}
        last, prov = 1.0, self.prov(time.time())
        for key, target in (("minimumFee", 1008), ("economyFee", 144), ("hourFee", 6), ("halfHourFee", 3), ("fastestFee", 1)):
            v, prov = await self.estimatesmartfee(target)
            rate = (v or {}).get("feerate")
            last = rates[key] = round(float(rate) * 1e5, 3) if rate and rate > 0 else last
        return {k: rates[k] for k in ("fastestFee", "halfHourFee", "hourFee", "economyFee", "minimumFee")}, prov
