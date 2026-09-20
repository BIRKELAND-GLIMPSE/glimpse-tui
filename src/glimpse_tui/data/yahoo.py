"""Yahoo Finance's public chart API: the endpoints the `yfinance` library reads, called directly.

Keyless. Checked live on 19 Sep 2026 with the terminal's own User-Agent (a browser User-Agent was refused). It carries
what no other open source does: index levels the minute they print (S&P 500, Nasdaq 100, Dow, FTSE, DAX, Nikkei, Hang
Seng, Kospi, Nifty), the VIX, the dollar index, Treasury yields, COMEX gold and silver, crude, gas and copper futures,
FX, and every US share and ETF. Yahoo's terms allow personal use; the terminal fetches for the person running it,
shows the source on every number, and stores nothing beyond its cache. `SET sources.yahoo false` turns it off.

Two calls. `spark` quotes up to 20 symbols in one request with 30 daily closes each; `chart` is one symbol's history.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from urllib.parse import quote

from .core import DELAYED, Provenance, Source, SourceError

BATCH = 20                  # spark refuses more symbols than this (HTTP 400 at 25)
CLOSED_AFTER_S = 1800       # no print for half an hour: its market is shut


@dataclass(frozen=True)
class Spark:
    symbol: str
    price: float
    prev: float | None          # the previous session's close: where the change is measured from
    high: float | None
    low: float | None
    at: float                   # the time of the last print
    closes: tuple[float, ...]   # daily closes, oldest first
    name: str = ""

    @property
    def closed(self) -> bool:
        return time.time() - self.at > CLOSED_AFTER_S


def _num(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def parse_spark(doc: dict) -> dict[str, Spark]:
    out: dict[str, Spark] = {}
    for row in (doc.get("spark") or {}).get("result") or []:
        try:
            r = row["response"][0]
            m = r["meta"]
            price = _num(m.get("regularMarketPrice"))
            if price is None:
                continue
            closes = [c for c in ((r.get("indicators") or {}).get("quote") or [{}])[0].get("close") or [] if c is not None]
            prev = _num(m.get("previousClose")) or _num(m.get("chartPreviousClose"))
            if closes and len(closes) >= 2 and abs(closes[-1] - price) / price < 0.02:
                prev = closes[-2]                      # the last daily close is today's: the one before it is yesterday's
            out[row["symbol"]] = Spark(row["symbol"], price, prev, _num(m.get("regularMarketDayHigh")),
                                       _num(m.get("regularMarketDayLow")), float(m.get("regularMarketTime") or 0),
                                       tuple(float(c) for c in closes), str(m.get("shortName") or m.get("longName") or ""))
        except (KeyError, IndexError, TypeError):
            continue
    return out


class Yahoo(Source):
    def __init__(self) -> None:
        super().__init__(name="yahoo", base_url="https://query1.finance.yahoo.com", delay=DELAYED, rate=1.0, burst=3,
                         serves="index levels, yields, futures, FX, US shares and ETFs, as Yahoo Finance shows them")

    async def spark(self, symbols: list[str]) -> tuple[dict[str, Spark], Provenance]:
        """The last price, the previous close, the day's range and 30 daily closes for each symbol, 20 to a request."""
        out: dict[str, Spark] = {}
        at = time.time()
        for i in range(0, len(symbols), BATCH):
            chunk = sorted(set(symbols[i:i + BATCH]))
            doc, at = await self.get("/v7/finance/spark", {"symbols": ",".join(chunk), "range": "1mo", "interval": "1d"}, ttl=60)
            out.update(parse_spark(doc if isinstance(doc, dict) else {}))
        if not out:
            raise SourceError("yahoo: no quotes in the answer")
        return out, self.prov(at, as_of=max(s.at for s in out.values()))

    async def history(self, symbol: str, days: int = 400) -> tuple[list[float], list[float], Provenance]:
        """Daily closes, oldest first."""
        rng = "1y" if days <= 250 else "2y" if days <= 500 else "10y" if days <= 2500 else "max"
        doc, at = await self.get(f"/v8/finance/chart/{quote(symbol, safe='')}", {"range": rng, "interval": "1d"}, ttl=3600, persist=True)
        try:
            r = doc["chart"]["result"][0]
            ts, closes = r["timestamp"], r["indicators"]["quote"][0]["close"]
        except (KeyError, IndexError, TypeError):
            raise SourceError(f"yahoo: no history for {symbol}") from None
        pts = [(float(t), float(c)) for t, c in zip(ts, closes, strict=False) if c is not None][-days:]
        if not pts:
            raise SourceError(f"yahoo: no history for {symbol}")
        return [p[0] for p in pts], [p[1] for p in pts], self.prov(at, as_of=pts[-1][0])
