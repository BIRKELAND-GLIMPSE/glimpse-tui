"""The data contract (TERMINAL.md 4.2): every value carries where it came from, and every host is treated well.

A `Source` is one host. It owns a token bucket, a memory cache, a slice of the SQLite cache for series and history,
exponential backoff with jitter on 429 and 5xx, and health counters that `SRC` shows. When a fetch fails the last
good value is served with its original fetch time, so a panel dims it and says how old it is instead of going blank.
"""
from __future__ import annotations

import asyncio
import json
import random
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from . import http

LIVE, DELAYED, DAILY, WEEKLY, MONTHLY = "live", "delayed", "daily", "weekly", "monthly"
STALE_AFTER = {LIVE: 90.0, DELAYED: 1800.0, DAILY: 4 * 86400.0, WEEKLY: 10 * 86400.0, MONTHLY: 45 * 86400.0}
FOREVER = 10 * 365 * 86400.0


@dataclass(frozen=True)
class Provenance:
    source: str             # "mempool.space", "bitview.space", "pyth", "fred:DGS10", "sec:0001050446"
    fetched_at: float       # unix seconds
    as_of: float            # the time the value describes
    delay: str              # live | delayed | daily | weekly | monthly
    note: str = ""          # a licence or attribution line, when a source asks for one

    def age(self, now: float | None = None) -> float:
        return (time.time() if now is None else now) - self.fetched_at

    def stale(self, now: float | None = None) -> bool:
        """Older than its kind allows: a live value after 90 s, a daily one after four days."""
        return self.age(now) > STALE_AFTER.get(self.delay, 90.0)

    def label(self) -> str:
        return f"{self.source} · {self.delay}"


def ago(seconds: float) -> str:
    s = int(max(seconds, 0))
    if s < 90:
        return f"{s}s"
    if s < 5400:
        return f"{s // 60}m"
    if s < 2 * 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


class SourceError(Exception):
    """A fetch failed and there was nothing cached to fall back on. The message is safe to show."""


class TokenBucket:
    """`rate` requests a second sustained, `burst` at once. `acquire` waits its turn; nothing is ever dropped."""

    def __init__(self, rate: float, burst: float = 1.0) -> None:
        self.rate, self.burst = rate, max(burst, 1.0)
        self.tokens, self.at = self.burst, time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        self.tokens, self.at = min(self.burst, self.tokens + (now - self.at) * self.rate), now

    @property
    def left(self) -> float:
        self._refill()
        return self.tokens

    async def acquire(self) -> None:
        async with self._lock:
            self._refill()
            if self.tokens < 1.0:
                await asyncio.sleep((1.0 - self.tokens) / self.rate)
                self._refill()
            self.tokens -= 1.0


@dataclass
class Health:
    ok: int = 0
    errors: int = 0
    cached: int = 0
    last_error: str = ""
    last_error_at: float = 0.0
    last_ok_at: float = 0.0
    latency_ms: float = 0.0
    paused_until: float = 0.0       # a 429 parks the host until its Retry-After has passed

    @property
    def state(self) -> str:
        if self.paused_until > time.time():
            return "limited"
        if not self.ok and not self.errors:
            return "idle"
        if self.last_error_at > self.last_ok_at:
            return "down"
        return "ok"


class DiskCache:
    """~/.cache/glimpse/terminal.db: series and history survive a restart, so <GO> paints from cache."""

    def __init__(self, path: str | None) -> None:
        self.path, self._db = path, None

    def _conn(self) -> sqlite3.Connection | None:
        if self._db is None and self.path:
            try:
                if self.path != ":memory:":
                    from pathlib import Path
                    Path(self.path).parent.mkdir(parents=True, exist_ok=True)
                self._db = sqlite3.connect(self.path, check_same_thread=False)
                self._db.execute("create table if not exists cache (k text primary key, fetched_at real, expires real, body text)")
            except (OSError, sqlite3.Error):
                self.path = None
        return self._db

    def get(self, key: str) -> tuple[float, float, Any] | None:
        db = self._conn()
        if db is None:
            return None
        try:
            row = db.execute("select fetched_at, expires, body from cache where k = ?", (key,)).fetchone()
            return (row[0], row[1], json.loads(row[2])) if row else None
        except (sqlite3.Error, ValueError):
            return None

    def put(self, key: str, fetched_at: float, expires: float, value: Any) -> None:
        db = self._conn()
        if db is None:
            return
        try:
            db.execute("insert or replace into cache values (?, ?, ?, ?)", (key, fetched_at, expires, json.dumps(value)))
            db.commit()
        except (sqlite3.Error, TypeError, ValueError):
            pass


_disk = DiskCache(None)


def use_disk_cache(path: str | None) -> None:
    global _disk
    _disk = DiskCache(path)


@dataclass
class Source:
    """One host. Subclasses add the endpoints; this class is the manners."""
    name: str                       # what the panel shows: "mempool.space"
    base_url: str
    delay: str = LIVE
    rate: float = 1.0               # sustained requests a second
    burst: float = 3.0
    note: str = ""                  # attribution, when asked for
    user_agent: str = ""
    serves: str = ""                # one line for SRC
    retries: int = 2
    timeout: float = 0.0            # seconds; 0 keeps the pool's default. The Treasury takes 18 s to answer.
    health: Health = field(default_factory=Health)

    def __post_init__(self) -> None:
        self.bucket = TokenBucket(self.rate, self.burst)
        self._mem: dict[str, tuple[float, float, Any]] = {}     # key -> (fetched_at, expires, value)
        self._inflight: dict[str, asyncio.Future] = {}

    # configuration ──────────────────────────────────────────

    def point_at(self, base_url: str) -> None:
        """Switch to a self-hosted backend without a restart. The cache goes: it described another node."""
        base_url = base_url.rstrip("/")
        if base_url != self.base_url:
            self.base_url, self.health = base_url, Health()
            self._mem.clear()

    @property
    def host(self) -> str:
        return self.base_url.split("://", 1)[-1].split("/", 1)[0]

    def prov(self, fetched_at: float, as_of: float | None = None, delay: str | None = None, source: str | None = None) -> Provenance:
        return Provenance(source or self.name, fetched_at, fetched_at if as_of is None else as_of, delay or self.delay, self.note)

    # fetching ───────────────────────────────────────────────

    def _key(self, path: str, params: dict | None) -> str:
        return f"{self.base_url}{path}?{json.dumps(params, sort_keys=True) if params else ''}"

    def cached(self, path: str, params: dict | None = None) -> tuple[Any, float] | None:
        hit = self._mem.get(self._key(path, params))
        return (hit[2], hit[0]) if hit else None

    async def get(self, path: str, params: dict | None = None, ttl: float = 30.0, persist: bool = False,
                  text: bool = False) -> tuple[Any, float]:
        """(value, fetched_at). Fresh from cache within `ttl`; otherwise fetched, politely. On failure the last good
        value comes back with its old fetch time. `persist` also keeps it in SQLite (series, history, filings)."""
        key, now = self._key(path, params), time.time()
        hit = self._mem.get(key)
        if hit is None and persist and (disk := _disk.get(key)):
            hit = self._mem[key] = disk
        if hit and hit[1] > now:
            self.health.cached += 1
            return hit[2], hit[0]
        if key in self._inflight:                       # two panes asking for the same thing share one request
            return await asyncio.shield(self._inflight[key])
        fut = self._inflight[key] = asyncio.get_running_loop().create_future()
        try:
            value = await self._fetch(path, params, text)
        except Exception as e:
            self._inflight.pop(key, None)
            msg = e.args[0] if isinstance(e, SourceError) else f"{self.name}: {type(e).__name__}"
            self.health.errors += 1
            self.health.last_error, self.health.last_error_at = msg, time.time()
            if hit:
                fut.set_result((hit[2], hit[0]))
                return hit[2], hit[0]                   # the last good value, with its age
            err = SourceError(msg)
            fut.set_exception(err)
            fut.exception()                             # mark retrieved: nobody else may be waiting
            raise err from None
        got = time.time()
        self._mem[key] = (got, got + ttl, value)
        if len(self._mem) > 600:
            for k in sorted(self._mem, key=lambda k: self._mem[k][1])[:200]:
                del self._mem[k]
        if persist:
            _disk.put(key, got, got + ttl, value)
        self._inflight.pop(key, None)
        fut.set_result((value, got))
        return value, got

    async def _fetch(self, path: str, params: dict | None, text: bool) -> Any:
        if self.health.paused_until > time.time():
            raise SourceError(f"{self.name}: rate limited, resting {int(self.health.paused_until - time.time())}s")
        last = ""
        for attempt in range(self.retries + 1):
            await self.bucket.acquire()
            t0 = time.monotonic()
            try:
                kw = {"timeout": self.timeout} if self.timeout else {}
                r = await http.client(self.base_url, self.user_agent).get(self.base_url + path, params=params, **kw)
            except httpx.HTTPError as e:
                last = f"{self.name}: {type(e).__name__}"
            else:
                self.health.latency_ms = (time.monotonic() - t0) * 1000
                if r.status_code == 200:
                    self.health.ok += 1
                    self.health.last_ok_at = time.time()
                    return r.text if text else r.json()
                last = f"{self.name}: HTTP {r.status_code}"
                if r.status_code == 429:
                    wait = _retry_after(r, 30.0 * (attempt + 1))
                    self.health.paused_until = time.time() + wait
                    raise SourceError(f"{self.name}: rate limited, resting {int(wait)}s")
                if r.status_code < 500:                 # 4xx: asking again will not help. A short plain-text reason is worth showing.
                    why = r.text.strip()[:80] if "html" not in r.headers.get("content-type", "") and len(r.text) < 300 else ""
                    raise SourceError(f"{last} {why}".strip())
            if attempt < self.retries:
                await asyncio.sleep(min(0.5 * 2 ** attempt, 8.0) * (0.5 + random.random()))
        raise SourceError(last)


def _retry_after(r: httpx.Response, default: float) -> float:
    try:
        return max(1.0, min(float(r.headers.get("retry-after", default)), 900.0))
    except ValueError:
        return default
