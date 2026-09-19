"""Bitview, the hosted Bitcoin Research Kit: tens of thousands of on-chain series, the UTXO realized price
distribution, and a BTC/USD price read from the chain alone. MIT, no key, self-hostable (`bitviewd`, localhost:3110).

The project's rule, and this module's: never invent a series identifier. Names come from `search` or from
`data/onchain.toml`, where each one was found in the catalogue and checked in Phase 0 (SOURCES.md).

A series has no timestamps. On the `day1` index position p is 2009-01-01 plus p days, and the last point is
today, still forming. Shapes: tests/fixtures/sources/bitview/.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from typing import Any

from .core import DAILY, LIVE, Provenance, SourceError
from .mempool import Mempool

DAY0 = datetime(2009, 1, 1, tzinfo=UTC).timestamp()
DAY_INDEXES = ("day1", "day3", "week1", "month1", "month3", "month6", "year1", "year10")


@dataclass
class SeriesData:
    name: str
    index: str
    start: int                  # position of the first value
    values: list[float | None]
    kind: str                   # Bitview's value type: Dollars, StoredF32, Bitcoin, …
    prov: Provenance

    @property
    def times(self) -> list[float]:
        """Unix seconds for each value on the day1 index; positions on any other index."""
        if self.index == "day1":
            return [DAY0 + (self.start + i) * 86400.0 for i in range(len(self.values))]
        return [float(self.start + i) for i in range(len(self.values))]

    @property
    def last(self) -> float | None:
        return next((v for v in reversed(self.values) if v is not None), None)

    def settled(self) -> float | None:
        """The last complete day: the final day1 point is today and still moving."""
        vals = [v for v in self.values[:-1] if v is not None] if self.index == "day1" and len(self.values) > 1 else []
        return vals[-1] if vals else self.last


def _num(v: Any) -> float | None:
    if isinstance(v, int | float) and not isinstance(v, bool):
        return float(v)
    if isinstance(v, list | tuple) and v:           # some height series carry several values a row: take the last (the close)
        return _num(v[-1])
    return None


class Bitview(Mempool):
    """The series API, plus the mempool-compatible explorer paths it serves (so it is a fallback Bitcoin backend)."""

    def __init__(self, base_url: str = "https://bitview.space") -> None:
        super().__init__(base_url=base_url, name="bitview.space", rate=2.0)
        self.serves = "on-chain series, URPD, the chain price oracle, mempool-compatible explorer paths"

    # the catalogue ──────────────────────────────────────────

    async def search(self, words: str, limit: int = 60) -> tuple[list[str], Provenance]:
        v, at = await self.get("/api/series/search", {"q": words, "limit": limit}, ttl=3600, persist=True)
        return [str(x) for x in v][:limit], self.prov(at, delay=DAILY)

    async def info(self, name: str) -> tuple[dict[str, Any], Provenance]:
        """{description, indexes, type}. A 404 means the name is not in the catalogue: say so, never guess another."""
        v, at = await self.get(f"/api/series/{name}", ttl=86400, persist=True)
        return v, self.prov(at, delay=DAILY)

    async def count(self) -> tuple[int, Provenance]:
        v, at = await self.get("/api/series/count", ttl=86400, persist=True)
        n = v.get("distinct", 0) if isinstance(v, dict) else int(v)
        return int(n), self.prov(at, delay=DAILY)

    # data ───────────────────────────────────────────────────

    def _series(self, name: str, index: str, env: dict[str, Any], at: float) -> SeriesData:
        vals = [_num(x) for x in env.get("data", [])]
        start = int(env.get("start", 0) or 0)
        as_of = DAY0 + (start + len(vals) - 1) * 86400.0 if index == "day1" and vals else at
        delay = DAILY if index in DAY_INDEXES else LIVE
        return SeriesData(name, index, start, vals, str(env.get("type", "")), Provenance(self.name, at, as_of, delay))

    async def series(self, name: str, index: str = "day1", start: int | str = -400, end: int | str | None = None) -> SeriesData:
        params: dict[str, Any] = {"start": start}
        if end is not None:
            params["end"] = end
        ttl = 3600 if index in DAY_INDEXES else 60
        env, at = await self.get(f"/api/series/{name}/{index}", params, ttl=ttl, persist=True)
        if not isinstance(env, dict) or "data" not in env:
            raise SourceError(f"{self.name}: {name} has no {index} data")
        return self._series(name, index, env, at)

    async def bulk(self, names: list[str], index: str = "day1", start: int | str = -400) -> list[SeriesData]:
        """Several series in one request, in the order asked for."""
        envs, at = await self.get("/api/series/bulk", {"series": ",".join(names), "index": index, "start": start}, ttl=3600, persist=True)
        if not isinstance(envs, list) or len(envs) != len(names):
            raise SourceError(f"{self.name}: bulk answered {len(envs) if isinstance(envs, list) else 0} of {len(names)} series")
        return [self._series(n, index, e, at) for n, e in zip(names, envs, strict=True)]

    # URPD and the oracle ────────────────────────────────────

    async def urpd_cohorts(self) -> tuple[list[str], Provenance]:
        v, at = await self.get("/api/urpd", ttl=86400, persist=True)
        names = [c if isinstance(c, str) else c.get("cohort", c.get("name", "")) for c in (v if isinstance(v, list) else v.get("cohorts", []))]
        return [n for n in names if n], self.prov(at, delay=DAILY)

    async def urpd_dates(self, cohort: str) -> tuple[list[str], Provenance]:
        v, at = await self.get(f"/api/urpd/{cohort}/dates", ttl=3600, persist=True)
        return [str(d) for d in v], self.prov(at, delay=DAILY)

    async def urpd(self, cohort: str = "all", date: str = "", agg: str = "log200") -> tuple[dict[str, Any], Provenance]:
        """Always aggregated: the raw distribution is 9,000 buckets and 900 KB."""
        path = f"/api/urpd/{cohort}/{date}" if date else f"/api/urpd/{cohort}"
        v, at = await self.get(path, {"agg": agg}, ttl=3600, persist=True)
        try:
            as_of = datetime.strptime(str(v.get("date", ""))[:10], "%Y-%m-%d").replace(tzinfo=UTC).timestamp()
        except ValueError:
            as_of = at
        return v, Provenance(self.name, at, as_of, DAILY)

    async def oracle_price(self) -> tuple[float, Provenance]:
        v, at = await self.get("/api/oracle/price", ttl=30)
        return float(v), self.prov(at)

    async def oracle_histogram(self, kind: str = "payments") -> tuple[list[int], Provenance]:
        """2,400 bins, bin = round(log10(sats) × 200). `payments` shows the round-dollar spikes; `outputs` is mostly dust."""
        v, at = await self.get(f"/api/oracle/histogram/{kind}/live", ttl=30)
        return [int(x) for x in v], self.prov(at)


def oracle_bin(usd: float, price: float) -> int:
    """The histogram bin where an output worth `usd` dollars lands at `price`."""
    import math
    return round(math.log10(usd * 1e8 / price) * 200) if usd > 0 and price > 0 else 0


def onchain_catalogue() -> dict[str, Any]:
    """`data/onchain.toml`: the identifiers Phase 0 found and checked. {"metrics": {...}, "waves": [...]}"""
    try:
        return tomllib.loads(resources.files("glimpse_tui.data").joinpath("onchain.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return {"metrics": {}, "waves": []}
