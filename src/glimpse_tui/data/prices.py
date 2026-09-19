"""Public exchange tickers: Coinbase Exchange, Kraken and Bitstamp. No key, no account, no Binance.

BTC is a composite: the median of the three last trades, so one venue's glitch or outage never moves the tape.
Kraken also serves spot FX pairs and tokenised equities (xStocks) on the same public ticker; a tokenised share is
a proxy for the stock and the panels label it so. Shapes: tests/fixtures/sources/{coinbase,kraken,bitstamp}/.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any

from .core import LIVE, Provenance, Source, SourceError


@dataclass
class Tick:
    price: float
    open: float | None      # where the change is measured from: Kraken's UTC-day open, Coinbase's and Bitstamp's 24 h open
    high: float | None
    low: float | None
    prov: Provenance
    volume: float | None = None


class Coinbase(Source):
    def __init__(self) -> None:
        super().__init__(name="coinbase", base_url="https://api.exchange.coinbase.com", delay=LIVE, rate=1.0, burst=4,
                         serves="spot crypto tickers, 24 h stats and candles")

    async def tick(self, product: str) -> Tick:
        t, at = await self.get(f"/products/{product}/ticker", ttl=8)
        s, _ = await self.get(f"/products/{product}/stats", ttl=60)
        return Tick(float(t["price"]), _f(s.get("open")), _f(s.get("high")), _f(s.get("low")), self.prov(at), _f(t.get("volume")))

    async def candles(self, product: str, granularity: int = 86400) -> tuple[list[list[float]], Provenance]:
        """[[time, low, high, open, close, volume], …], newest first, at most 300."""
        v, at = await self.get(f"/products/{product}/candles", {"granularity": granularity}, ttl=min(granularity / 4, 900), persist=True)
        return v, self.prov(at)


class Kraken(Source):
    def __init__(self) -> None:
        super().__init__(name="kraken", base_url="https://api.kraken.com", delay=LIVE, rate=1.0, burst=3,
                         serves="spot crypto, spot FX pairs and tokenised equities (xStocks)")

    async def ticks(self, pairs: list[str]) -> dict[str, Tick]:
        """One request for every pair. Keys are Kraken's own result names (XXBTZUSD, ZEURZUSD, SPYxUSD)."""
        out: dict[str, Tick] = {}
        tokens = sorted({p for p in pairs if is_tokenised(p)})
        spot = sorted({p for p in pairs if not is_tokenised(p)})
        for group, extra in ((spot, {}), (tokens, {"asset_class": "tokenized_asset"})):     # xStocks need their asset class named
            if not group:
                continue
            v, at = await self.get("/0/public/Ticker", {"pair": ",".join(group), **extra}, ttl=8 if not extra else 20)
            if v.get("error"):
                raise SourceError(f"kraken: {v['error'][0]}")
            for key, r in v.get("result", {}).items():
                out[key] = Tick(float(r["c"][0]), _f(r.get("o")), _f(r["h"][0]), _f(r["l"][0]), self.prov(at), _f(r["v"][1]))
        return out

    async def ohlc(self, pair: str, interval: int = 1440) -> tuple[list[list[Any]], Provenance]:
        """[[time, open, high, low, close, vwap, volume, count], …], oldest first, at most 720."""
        extra = {"asset_class": "tokenized_asset"} if is_tokenised(pair) else {}
        v, at = await self.get("/0/public/OHLC", {"pair": pair, "interval": interval, **extra}, ttl=min(interval * 15, 900), persist=True)
        if v.get("error"):
            raise SourceError(f"kraken: {v['error'][0]}")
        rows = next((r for k, r in v.get("result", {}).items() if k != "last"), [])
        return rows, self.prov(at)


class Bitstamp(Source):
    def __init__(self) -> None:
        super().__init__(name="bitstamp", base_url="https://www.bitstamp.net", delay=LIVE, rate=1.0, burst=3,
                         serves="spot crypto tickers")

    async def tick(self, pair: str) -> Tick:
        t, at = await self.get(f"/api/v2/ticker/{pair}/", ttl=8)
        return Tick(float(t["last"]), _f(t.get("open_24")), _f(t.get("high")), _f(t.get("low")), self.prov(at), _f(t.get("volume")))


def is_tokenised(pair: str) -> bool:
    """Kraken's xStocks pairs are the ticker, a lower-case x, then the quote: `SPYxUSD`."""
    return len(pair) > 4 and pair[-4] == "x" and pair[-3:].isupper()


def _f(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def composite(ticks: dict[str, Tick]) -> Tick | None:
    """The median of the venues that answered. One venue alone still gives a price; the panel names which."""
    live = {k: t for k, t in ticks.items() if t and t.price > 0}
    if not live:
        return None
    med = lambda xs: statistics.median(xs) if xs else None          # noqa: E731
    newest = max(live.values(), key=lambda t: t.prov.fetched_at).prov
    prov = Provenance("+".join(sorted(live)), newest.fetched_at, newest.as_of, LIVE)
    return Tick(med([t.price for t in live.values()]), med([t.open for t in live.values() if t.open]),
                max((t.high for t in live.values() if t.high), default=None), min((t.low for t in live.values() if t.low), default=None), prov)
