"""FRED without a key: one CSV per series from fredgraph.csv. A free key would add the API and the release
calendar, but nothing here needs it.

The CSV's header is `observation_date,<SERIES>`; a missing observation is a lone `.`. Units differ by series and
FRED does not put them in the file, so the ones the terminal converts are written down here and tested (MACRO).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from .core import DAILY, Provenance, Source

URL = "https://fred.stlouisfed.org"
# What FRED publishes each series in. WALCL and WTREGEN are millions of dollars, RRPONTSYD is billions (TERMINAL.md 7).
UNITS = {"WALCL": 1e6, "WTREGEN": 1e6, "RRPONTSYD": 1e9, "M2SL": 1e9, "WRESBAL": 1e6}
# Third-party series FRED marks as copyrighted. They are fetched by the user's own machine and shown to that user,
# with the owner named on the panel. SOURCES.md has the reading of FRED's terms.
NOTES = {"SP500": "© S&P Dow Jones Indices LLC", "DJIA": "© S&P Dow Jones Indices LLC", "NASDAQ100": "© Nasdaq OMX Group",
         "NASDAQCOM": "© Nasdaq OMX Group", "NIKKEI225": "© Nikkei Inc."}


@dataclass
class Series:
    id: str
    dates: list[float]          # unix seconds, UTC midnight of the observation date
    values: list[float]         # missing observations dropped
    prov: Provenance

    @property
    def last(self) -> float | None:
        return self.values[-1] if self.values else None

    @property
    def prev(self) -> float | None:
        return self.values[-2] if len(self.values) > 1 else None

    def at_or_before(self, ts: float) -> float | None:
        hit = None
        for d, v in zip(self.dates, self.values, strict=True):
            if d > ts:
                break
            hit = v
        return hit


def parse_csv(text: str) -> tuple[list[float], list[float]]:
    dates, values = [], []
    for line in text.strip().splitlines()[1:]:
        day, _, raw = line.partition(",")
        raw = raw.split(",")[0].strip()
        if not raw or raw == ".":
            continue
        try:
            values.append(float(raw))
            dates.append(datetime.strptime(day.strip(), "%Y-%m-%d").replace(tzinfo=UTC).timestamp())
        except ValueError:
            continue
    return dates, values


class Fred(Source):
    def __init__(self) -> None:
        super().__init__(name="fred", base_url=URL, delay=DAILY, rate=1.0, burst=3,
                         serves="US rates, Fed balance sheet, M2, spot oil, FX and index daily closes")

    async def series(self, series_id: str, start: str = "") -> Series:
        """`start` is YYYY-MM-DD; without it FRED sends the whole history."""
        sid = series_id.upper()
        params = {"id": sid, **({"cosd": start} if start else {})}
        text, at = await self.get("/graph/fredgraph.csv", params, ttl=6 * 3600, persist=True, text=True)
        dates, values = parse_csv(text)
        prov = Provenance(f"fred:{sid}", at, dates[-1] if dates else at, DAILY, NOTES.get(sid, ""))
        return Series(sid, dates, values, prov)


def dollars(series_id: str, value: float) -> float:
    """A FRED value in plain dollars, whatever unit the series is published in."""
    return value * UNITS.get(series_id.upper(), 1.0)


def net_liquidity(walcl_m: float, wtregen_m: float, rrp_b: float) -> float:
    """WALCL − WTREGEN − RRPONTSYD in dollars. The first two arrive in millions, the third in billions."""
    return dollars("WALCL", walcl_m) - dollars("WTREGEN", wtregen_m) - dollars("RRPONTSYD", rrp_b)
