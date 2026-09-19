"""A small Electrum client: JSON lines over TLS or TCP on asyncio streams (SOURCES.md, "Electrum servers").

Electrum is a backend the user configures and nothing else: `bitcoin.electrum = "ssl://your-server:50002"`. There is
no default server, so an unconfigured client refuses every call and never opens a socket. Certificates are checked
like any other TLS connection. A self-signed server (most are) needs the URL to say so: `ssl+insecure://host:50002`.
With `socks5` set (Tor) the connection is made through the proxy, or not at all.

    ssl://host:50002            TLS, certificate verified
    ssl+insecure://host:50002   TLS, any certificate (your own server with a self-signed one)
    tcp://host:50001            plain TCP (a server on your own machine or LAN, or an onion through Tor)

An Electrum server indexes by script hash: sha256 of the output script, byte-reversed, in hex. `scripthash` turns
an address into one. Tests replay tests/fixtures/sources/electrum/*.jsonl through a fake stream: no sockets.
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import ssl
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

from . import http
from .core import LIVE, Health, Provenance, SourceError

CLIENT, PROTOCOL = "glimpse-tui/0.1", "1.4"
Streams = tuple[Any, Any]                   # (reader, writer): asyncio's, or a test's fakes with the same methods

# ── addresses to script hashes ──────────────────────────────

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _base58check(s: str) -> bytes:
    n = 0
    for ch in s:
        if ch not in _B58:
            raise ValueError("not base58")
        n = n * 58 + _B58.index(ch)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    raw = b"\x00" * (len(s) - len(s.lstrip("1"))) + raw
    body, check = raw[:-4], raw[-4:]
    if len(raw) < 5 or hashlib.sha256(hashlib.sha256(body).digest()).digest()[:4] != check:
        raise ValueError("bad base58 checksum")
    return body


def _polymod(values: list[int]) -> int:
    chk = 1
    for v in values:
        top = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ v
        for i, g in enumerate((0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)):
            chk ^= g if (top >> i) & 1 else 0
    return chk


def _segwit(addr: str) -> tuple[int, bytes]:
    """(witness version, program) of a bech32 (v0) or bech32m (v1+) address, checksum verified (BIP 173, BIP 350)."""
    if addr.lower() != addr and addr.upper() != addr:
        raise ValueError("mixed case")
    addr = addr.lower()
    hrp, _, data = addr.rpartition("1")
    if hrp not in ("bc", "tb", "bcrt") or len(data) < 7 or any(c not in _B32 for c in data):
        raise ValueError("not a segwit address")
    values = [_B32.index(c) for c in data]
    const = _polymod([ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp] + values)
    version = values[0]
    if const != (1 if version == 0 else 0x2BC830A3):
        raise ValueError("bad bech32 checksum")
    acc = bits = 0
    out = bytearray()
    for v in values[1:-6]:                                  # five-bit groups back into bytes
        acc, bits = (acc << 5) | v, bits + 5
        while bits >= 8:
            bits -= 8
            out.append((acc >> bits) & 0xFF)
    if bits >= 5 or (acc & ((1 << bits) - 1)):
        raise ValueError("bad padding")
    if version > 16 or not 2 <= len(out) <= 40 or (version == 0 and len(out) not in (20, 32)):
        raise ValueError("bad witness program")
    return version, bytes(out)


def script_pubkey(address: str) -> bytes:
    """The output script an address stands for: P2PKH, P2SH, and every segwit version."""
    try:
        if address.lower().startswith(("bc1", "tb1", "bcrt1")):
            version, program = _segwit(address)
            return bytes([0x50 + version if version else 0, len(program)]) + program
        body = _base58check(address)
        if len(body) == 21 and body[0] in (0x00, 0x6F):
            return b"\x76\xa9\x14" + body[1:] + b"\x88\xac"
        if len(body) == 21 and body[0] in (0x05, 0xC4):
            return b"\xa9\x14" + body[1:] + b"\x87"
    except ValueError as e:
        raise ValueError(f"not a Bitcoin address: {e}") from None
    raise ValueError("not a Bitcoin address")


def scripthash(address_or_script: str | bytes) -> str:
    """Electrum's index key: sha256 of the scriptPubKey, reversed, as hex."""
    script = address_or_script if isinstance(address_or_script, bytes) else script_pubkey(address_or_script)
    return hashlib.sha256(script).digest()[::-1].hex()


# ── the connection ──────────────────────────────────────────

def parse_url(url: str) -> tuple[str, str, int]:
    """(scheme, host, port). Only ssl, ssl+insecure and tcp are understood; the port defaults to Electrum's."""
    u = urlsplit(url.strip())
    if u.scheme not in ("ssl", "ssl+insecure", "tcp") or not u.hostname:
        raise SourceError("electrum: the URL must look like ssl://host:50002, ssl+insecure://host:50002 or tcp://host:50001")
    return u.scheme, u.hostname, u.port or (50001 if u.scheme == "tcp" else 50002)


def tls_context(scheme: str) -> ssl.SSLContext | None:
    if scheme == "tcp":
        return None
    ctx = ssl.create_default_context()
    if scheme == "ssl+insecure":                            # only ever because the URL asked for it
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


async def socks5_connect(reader: Any, writer: Any, host: str, port: int) -> None:
    """RFC 1928, no authentication, CONNECT by host name so the proxy (Tor) does the DNS lookup."""
    writer.write(b"\x05\x01\x00")
    await writer.drain()
    if await reader.readexactly(2) != b"\x05\x00":
        raise SourceError("electrum: the SOCKS5 proxy refused the handshake")
    name = host.encode("idna")
    writer.write(b"\x05\x01\x00\x03" + bytes([len(name)]) + name + port.to_bytes(2, "big"))
    await writer.drain()
    head = await reader.readexactly(4)
    if head[1] != 0:
        raise SourceError(f"electrum: the SOCKS5 proxy could not connect (code {head[1]})")
    size = {1: 4, 4: 16}.get(head[3])
    await reader.readexactly((size if size is not None else (await reader.readexactly(1))[0]) + 2)


async def open_streams(url: str, timeout: float = 10.0) -> Streams:
    """Open the socket the URL describes. Through the SOCKS5 proxy when one is set: never around it."""
    scheme, host, port = parse_url(url)
    ctx = tls_context(scheme)
    if proxy := http.socks5():
        p = urlsplit(proxy)
        reader, writer = await asyncio.wait_for(asyncio.open_connection(p.hostname, p.port or 9050), timeout)
        await asyncio.wait_for(socks5_connect(reader, writer, host, port), timeout)
        if ctx is not None:
            await asyncio.wait_for(writer.start_tls(ctx, server_hostname=host), timeout)
        return reader, writer
    return await asyncio.wait_for(asyncio.open_connection(host, port, ssl=ctx, server_hostname=host if ctx else None), timeout)


class Electrum:
    """One connection, one request in flight. Every public call returns `(value, Provenance)` like a `Source`."""

    serves = "tip, fee estimates, fee histogram, address balance, history and UTXOs (your own server)"

    def __init__(self, url: str = "", timeout: float = 10.0, opener: Callable[[str, float], Awaitable[Streams]] | None = None) -> None:
        self.base_url, self.timeout, self.name = url.strip(), timeout, "electrum"
        self.delay, self.health = LIVE, Health()
        self.server = ""                                    # what `server.version` said: "ElectrumX 1.18.0"
        self._open = opener or open_streams
        self._streams: Streams | None = None
        self._id = -1                                       # the first request is id 0, as in the recorded transcripts
        self._lock = asyncio.Lock()
        self.name = self._label()

    # configuration ──────────────────────────────────────────

    def point_at(self, url: str) -> None:
        url = url.strip()
        if url != self.base_url:
            self.base_url, self.health, self.server = url, Health(), ""
            self.name = self._label()
            self._drop()

    def _label(self) -> str:
        return self.host or "electrum"

    @property
    def host(self) -> str:
        try:
            return parse_url(self.base_url)[1] if self.base_url else ""
        except SourceError:                                 # a malformed URL is reported by the first call, not the constructor
            return ""

    @property
    def public(self) -> bool:
        """True when a lookup leaves the user's own machines: not loopback, not a private range, not an onion, no Tor."""
        h = self.host
        if not h or http.socks5() or h == "localhost" or h.endswith((".local", ".lan", ".onion")):
            return False
        try:
            ip = ipaddress.ip_address(h)
            return not (ip.is_private or ip.is_loopback or ip.is_link_local)
        except ValueError:
            return True

    def prov(self, at: float) -> Provenance:
        return Provenance(self.name, at, at, self.delay)

    # the wire ───────────────────────────────────────────────

    def _drop(self) -> None:
        if self._streams:
            try:
                self._streams[1].close()
            except Exception:
                pass
        self._streams = None

    async def close(self) -> None:
        self._drop()

    async def _connect(self) -> Streams:
        if not self.base_url:
            raise SourceError("electrum: no server configured. SET bitcoin.electrum ssl://your-server:50002")
        if self._streams is None:
            self._streams = await self._open(self.base_url, self.timeout)
            version = await self._exchange("server.version", [CLIENT, PROTOCOL])        # the protocol wants this first
            self.server = str(version[0]) if isinstance(version, list) and version else ""
        return self._streams

    async def _exchange(self, method: str, params: list[Any]) -> Any:
        reader, writer = self._streams                      # type: ignore[misc]
        self._id += 1
        want = self._id
        writer.write(json.dumps({"jsonrpc": "2.0", "id": want, "method": method, "params": params}).encode() + b"\n")
        await writer.drain()
        while True:
            raw = await asyncio.wait_for(reader.readline(), self.timeout)
            if not raw:
                raise ConnectionError("closed")
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(msg, dict) or msg.get("id") != want:     # a subscription's notification, or a stray reply
                continue
            if msg.get("error"):
                err = msg["error"]
                raise SourceError(f"{self.name}: {err.get('message', err) if isinstance(err, dict) else err}"[:160])
            return msg.get("result")

    async def call(self, method: str, *params: Any) -> tuple[Any, Provenance]:
        async with self._lock:
            t0 = time.monotonic()
            try:
                await self._connect()
                value = await self._exchange(method, list(params))
            except SourceError as e:
                self._fail(str(e))
                raise
            except (OSError, TimeoutError, asyncio.IncompleteReadError) as e:
                self._drop()                                # the next call reconnects
                hint = ". A self-signed server needs ssl+insecure:// in the URL" if isinstance(e, ssl.SSLCertVerificationError) else ""
                self._fail(f"{self.name}: {type(e).__name__}{hint}")
                raise SourceError(self.health.last_error) from None
            now = time.time()
            self.health.ok, self.health.last_ok_at, self.health.latency_ms = self.health.ok + 1, now, (time.monotonic() - t0) * 1000
            return value, self.prov(now)

    def _fail(self, msg: str) -> None:
        self.health.errors += 1
        self.health.last_error, self.health.last_error_at = msg, time.time()

    # the protocol, as recorded ──────────────────────────────

    async def version(self) -> tuple[list[str], Provenance]:
        """[server software, protocol version]. Sent again on an open connection it costs one round trip."""
        return await self.call("server.version", CLIENT, PROTOCOL)

    async def headers_subscribe(self) -> tuple[dict[str, Any], Provenance]:
        """The tip: {"height": 967731, "hex": <80-byte header>}. Later tips arrive as notifications and are skipped."""
        return await self.call("blockchain.headers.subscribe")

    async def fee_histogram(self) -> tuple[list[list[float]], Provenance]:
        """[[sat/vB, vsize], ...] from the highest fee rate down."""
        return await self.call("mempool.get_fee_histogram")

    async def estimate_fee(self, blocks: int) -> tuple[float | None, Provenance]:
        """sat/vB to confirm within `blocks`. The wire value is BTC per kB; -1 means the server has no estimate."""
        v, p = await self.call("blockchain.estimatefee", blocks)
        return (round(float(v) * 1e5, 3) if isinstance(v, int | float) and v > 0 else None), p

    async def get_balance(self, address: str) -> tuple[dict[str, int], Provenance]:
        """{"confirmed": sats, "unconfirmed": sats}."""
        return await self.call("blockchain.scripthash.get_balance", scripthash(address))

    async def get_history(self, address: str) -> tuple[list[dict[str, Any]], Provenance]:
        """[{"tx_hash", "height"}]; height 0 or -1 is unconfirmed; mempool rows also carry "fee"."""
        return await self.call("blockchain.scripthash.get_history", scripthash(address))

    async def listunspent(self, address: str) -> tuple[list[dict[str, Any]], Provenance]:
        """[{"tx_hash", "tx_pos", "height", "value"}]."""
        return await self.call("blockchain.scripthash.listunspent", scripthash(address))

    # the same capability names the REST backends answer to ──

    async def tip_height(self) -> tuple[int, Provenance]:
        v, p = await self.headers_subscribe()
        return int(v["height"]), p

    async def fees(self) -> tuple[dict[str, float], Provenance]:
        """The recommended-fees shape from five `estimatefee` calls; a missing estimate takes the next slower one."""
        rates: dict[str, float] = {}
        last, prov = 1.0, self.prov(time.time())
        for key, target in (("minimumFee", 1008), ("economyFee", 144), ("hourFee", 6), ("halfHourFee", 3), ("fastestFee", 1)):
            rate, prov = await self.estimate_fee(target)
            last = rates[key] = rate if rate is not None else last
        return {k: rates[k] for k in ("fastestFee", "halfHourFee", "hourFee", "economyFee", "minimumFee")}, prov

    async def mempool(self) -> tuple[dict[str, Any], Provenance]:
        hist, p = await self.fee_histogram()
        return {"vsize": sum(v for _, v in hist), "fee_histogram": hist}, p

    async def address_utxo(self, address: str) -> tuple[list[dict[str, Any]], Provenance]:
        """UTXOs in the Esplora shape, so ADDR can list them from the user's own server."""
        rows, p = await self.listunspent(address)
        return [{"txid": r["tx_hash"], "vout": r["tx_pos"], "value": r["value"],
                 "status": {"confirmed": r.get("height", 0) > 0, "block_height": r.get("height") or None}} for r in rows], p
