"""Hourly candles for the models: public Coinbase data (or Glimpse's own oracle for what no exchange quotes), paged,
cached on disk, complete bars only."""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

from ..api import ADJUSTMENT, COINBASE, HASHPRICE, ORACLE, PAIRS, RETARGET_SERIES, oracle_candles, oracle_spot, oracle_topic

HOURS = 2000                     # about twelve weeks: enough for a 30-day signal to have 1,000+ readings of history
PAGE = 300                       # Coinbase serves at most 300 candles a request
COLUMNS = ["open", "high", "low", "close", "volume"]
_MEM: dict[str, pd.DataFrame] = {}
_LOCKS: dict[str, asyncio.Lock] = {}


def cache_dir() -> Path:
    d = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "glimpse"
    d.mkdir(parents=True, exist_ok=True)
    return d


def frame(rows: list[list[float]]) -> pd.DataFrame:
    """Coinbase rows are [time, low, high, open, close, volume], newest first."""
    if not rows:
        return pd.DataFrame(columns=COLUMNS, index=pd.DatetimeIndex([], tz="UTC"), dtype=float)
    a = np.asarray(rows, dtype=float)
    df = pd.DataFrame({"open": a[:, 3], "high": a[:, 2], "low": a[:, 1], "close": a[:, 4], "volume": a[:, 5]},
                      index=pd.to_datetime(a[:, 0].astype("int64"), unit="s", utc=True))
    return df[~df.index.duplicated(keep="last")].sort_index()


def complete(df: pd.DataFrame, now: float, hours: int = HOURS) -> pd.DataFrame:
    """Closed bars only, on an unbroken hourly index. An hour nobody traded repeats the last close at zero volume."""
    if df.empty:
        return df
    last_open = pd.Timestamp((int(now) // 3600 - 1) * 3600, unit="s", tz="UTC")
    df = df[df.index <= last_open]
    if df.empty:
        return df
    idx = pd.date_range(df.index[0], df.index[-1], freq="h")
    out = df.reindex(idx)
    out["close"] = out["close"].ffill()
    for k in ("open", "high", "low"):
        out[k] = out[k].fillna(out["close"])
    out["volume"] = out["volume"].fillna(0.0)
    return out.iloc[-hours:].astype(float)


async def _fetch(client: httpx.AsyncClient, pair: str, start: int, end: int) -> list[list[float]]:
    iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))  # noqa: E731
    for attempt in range(3):
        r = await client.get(COINBASE.format(pair=pair) + "/candles", params={"granularity": 3600, "start": iso(start), "end": iso(end)})
        if r.status_code == 429:
            await asyncio.sleep(1.0 + attempt)
            continue
        r.raise_for_status()
        return r.json()
    return []


async def _fetch_oracle(topic: int, start: int, end: int) -> list[list[float]]:
    """The oracle's hourly candles as Coinbase rows, [time, low, high, open, close, volume], for `frame`. No volume:
    the oracle is a price, not a venue."""
    return [[c.t, c.l, c.h, c.o, c.c, 0.0] for c in await oracle_candles(topic, 3600, start=start, end=end)]


async def hourly_bars(asset: str, hours: int = HOURS, now: float | None = None) -> pd.DataFrame:
    """Complete hourly bars, oldest first. The first call pages through history; later calls fetch only the new hours.
    An asset no exchange quotes (WTI/BTC) reads Glimpse's own oracle, which answers a whole history in one request."""
    pair = PAIRS.get(asset)
    if not pair and asset not in ORACLE:
        raise ValueError(f"no price feed for {asset}")
    now = time.time() if now is None else now
    async with _LOCKS.setdefault(asset, asyncio.Lock()):
        have = _MEM.get(asset)
        path = cache_dir() / f"bars-{asset}-1h.csv"
        if have is None and path.is_file():
            try:
                have = pd.read_csv(path, index_col=0, parse_dates=True)
                have.index = pd.DatetimeIndex(have.index, tz="UTC") if have.index.tz is None else have.index
            except Exception:
                have = None
        want_last = (int(now) // 3600 - 1) * 3600
        if have is not None and len(have) and int(have.index[-1].timestamp()) >= want_last and len(have) >= min(hours, HOURS) - 24:
            _MEM[asset] = have
            return have.iloc[-hours:]
        start = int(now) - hours * 3600
        if have is not None and len(have) >= hours - 24:
            start = max(start, int(have.index[-1].timestamp()) - 3600)
        parts = [have] if have is not None and start > int(now) - hours * 3600 else []
        topic = await oracle_topic(asset) if asset in ORACLE else 0
        page = HOURS if topic else PAGE
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "glimpse-tui"}) as c:
            t, last = start, int(now)
            while t < last:
                until = min(t + page * 3600, last)
                parts.append(frame(await (_fetch_oracle(topic, t, until) if topic else _fetch(c, pair, t, until))))
                t += page * 3600
                await asyncio.sleep(0.12)
        df = pd.concat([p for p in parts if p is not None and len(p)]) if parts else frame([])
        df = complete(df[~df.index.duplicated(keep="last")].sort_index(), now, max(hours, HOURS))
        if len(df):
            _MEM[asset] = df
            try:
                df.to_csv(path)
            except OSError:
                pass
        return df.iloc[-hours:]


async def spot_price(asset: str) -> float | None:
    if asset in ORACLE:
        try:
            return await oracle_spot(await oracle_topic(asset)) or None
        except Exception:
            return None
    pair = PAIRS.get(asset)
    if not pair:
        return None
    try:
        async with httpx.AsyncClient(timeout=8, headers={"User-Agent": "glimpse-tui"}) as c:
            r = await c.get(COINBASE.format(pair=pair) + "/ticker")
            return float(r.json()["price"])
    except Exception:
        return None


MIN_FEED_BARS = 400


class Feed:
    """One asset's bars and spot, shared by every bot and by the bots screen. The volatility fit and each model's
    cached signals are kept for as long as the last complete bar is unchanged, so a new close costs almost nothing."""

    def __init__(self, asset: str) -> None:
        self.asset = asset
        self.bars: pd.DataFrame | None = None
        self.spot = 0.0
        self.fetched_at = 0.0
        self._key = None
        self._scale = None
        self._cache: dict = {}
        self.parts = None                       # hashprice.Inputs or difficulty.Inputs on those series: what their own bots read

    @property
    def ready(self) -> bool:
        if self.asset in RETARGET_SERIES:       # no change, the adjustment's spot, is zero
            return self.bars is not None and self.parts is not None and self.parts.ready
        return self.bars is not None and len(self.bars) > 0 and self.spot > 0

    async def _refresh_retarget(self) -> None:
        """A retarget series has no price to walk. Its only bars are the difficulty's own, which only name the hour; its spot is
        the level at which nothing changes: today's difficulty, or a 0% adjustment."""
        from ..difficulty import shared_inputs

        self.parts = shared_inputs()
        await self.parts.refresh()
        if self.parts.ts is not None:
            self.bars = self.parts.hourly_bars(MIN_FEED_BARS)
            self.spot = 0.0 if self.asset == ADJUSTMENT else float(self.parts.diff[-1]) / 1e12
        self.fetched_at = time.time()

    async def refresh(self, max_age: float = 30.0) -> None:
        if time.time() - self.fetched_at < max_age and self.ready:
            return
        if self.asset in RETARGET_SERIES:
            await self._refresh_retarget()
            return
        if self.asset == HASHPRICE:
            from ..hashprice import Inputs

            self.parts = self.parts or Inputs()
            await self.parts.refresh()
        bars, spot = await asyncio.gather(hourly_bars(self.asset), spot_price(self.asset))
        if len(bars):
            self.bars = bars
        if spot:
            self.spot = spot
        elif self.bars is not None and len(self.bars) and self.spot <= 0:
            self.spot = float(self.bars["close"].iloc[-1])
        self.fetched_at = time.time()

    def ctx(self, bins: list[tuple[float, float]], end: float, now: float | None = None, height: int = 0):
        from .core import Ctx, Scale

        if self.bars is None:
            raise RuntimeError("feed not loaded")
        key = self.bars.index[-1]
        if key != self._key:
            self._key, self._cache = key, {}
            self._scale = Scale.still() if self.asset in RETARGET_SERIES else Scale.fit(self.bars, asset=self.asset)
        edges = np.array([bins[0][0]] + [hi for _, hi in bins], dtype=float)
        return Ctx(bars=self.bars, spot=self.spot, edges=edges, now=time.time() if now is None else now, end=float(end),
                   asset=self.asset, scale=self._scale, cache=self._cache, parts=self.parts, height=height)
