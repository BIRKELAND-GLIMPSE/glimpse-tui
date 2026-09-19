"""ECB euro foreign exchange reference rates: one official fix a TARGET business day, about 16:00 CET.

They are reference rates, not tradable quotes, and they are a day old by the time most people read them. Panels
say `daily` and show the fix date. Everything is quoted per euro; `cross` turns that into the pair a trader names.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from .core import DAILY, Provenance, Source

_DAY = re.compile(r"<Cube time=['\"](\d{4}-\d{2}-\d{2})['\"]>(.*?)</Cube>", re.S)
_RATE = re.compile(r"currency=['\"]([A-Z]{3})['\"] rate=['\"]([0-9.]+)['\"]")


@dataclass
class Fix:
    date: float
    per_eur: dict[str, float]           # USD -> 1.1460

    def cross(self, pair: str) -> float | None:
        """`USDJPY` is yen per dollar, `EURUSD` dollars per euro, `GBPUSD` dollars per pound."""
        base, quote = pair[:3].upper(), pair[3:6].upper()
        rate = {**self.per_eur, "EUR": 1.0}
        if base not in rate or quote not in rate or not rate[base]:
            return None
        return rate[quote] / rate[base]


def parse(xml: str) -> list[Fix]:
    """Every day in the file, oldest first."""
    out = []
    for day, body in _DAY.findall(xml):
        rates = {c: float(r) for c, r in _RATE.findall(body)}
        if rates:
            out.append(Fix(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC).timestamp(), rates))
    return sorted(out, key=lambda f: f.date)


class Ecb(Source):
    def __init__(self) -> None:
        super().__init__(name="ecb", base_url="https://www.ecb.europa.eu", delay=DAILY, rate=0.5, burst=2,
                         note="ECB euro reference rates", serves="official daily FX reference rates against the euro")

    async def fixes(self, history: bool = True) -> tuple[list[Fix], Provenance]:
        """The last 90 days of fixes (one 40 KB file), or just the latest."""
        path = "/stats/eurofxref/eurofxref-hist-90d.xml" if history else "/stats/eurofxref/eurofxref-daily.xml"
        xml, at = await self.get(path, ttl=3600, persist=True, text=True)
        days = parse(xml)
        return days, Provenance(self.name, at, days[-1].date if days else at, DAILY, self.note)


DXY_WEIGHTS = {"EURUSD": -0.576, "USDJPY": 0.136, "GBPUSD": -0.119, "USDCAD": 0.091, "USDSEK": 0.042, "USDCHF": 0.036}
DXY_CONSTANT = 50.14348112


def dxy(rates: dict[str, float]) -> float | None:
    """ICE's US Dollar Index formula: 50.14348112 × EURUSD^-0.576 × USDJPY^0.136 × GBPUSD^-0.119 × USDCAD^0.091 ×
    USDSEK^0.042 × USDCHF^0.036. Computed here from whatever each leg's best source is, and labelled `computed`."""
    v = DXY_CONSTANT
    for pair, w in DXY_WEIGHTS.items():
        r = rates.get(pair)
        if not r or r <= 0:
            return None
        v *= r ** w
    return v
