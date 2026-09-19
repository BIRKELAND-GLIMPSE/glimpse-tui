"""The official US Treasury par yield curve, real yields and bill rates: keyless XML from home.treasury.gov.

The feed is Atom with OData properties: one <entry> a business day, `d:NEW_DATE` and one `d:BC_*` per tenor.
A month of it is 20 KB, a year 250 KB; both are cached until the next business day's file could exist.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from .core import DAILY, Provenance, Source

TENORS = (("1M", "BC_1MONTH", 1 / 12), ("2M", "BC_2MONTH", 2 / 12), ("3M", "BC_3MONTH", 0.25), ("4M", "BC_4MONTH", 4 / 12),
          ("6M", "BC_6MONTH", 0.5), ("1Y", "BC_1YEAR", 1.0), ("2Y", "BC_2YEAR", 2.0), ("3Y", "BC_3YEAR", 3.0), ("5Y", "BC_5YEAR", 5.0),
          ("7Y", "BC_7YEAR", 7.0), ("10Y", "BC_10YEAR", 10.0), ("20Y", "BC_20YEAR", 20.0), ("30Y", "BC_30YEAR", 30.0))
REAL_TENORS = (("5Y", "TC_5YEAR"), ("7Y", "TC_7YEAR"), ("10Y", "TC_10YEAR"), ("20Y", "TC_20YEAR"), ("30Y", "TC_30YEAR"))
_ENTRY = re.compile(r"<m:properties>(.*?)</m:properties>", re.S)
_PROP = re.compile(r"<d:([A-Z0-9_]+)[^>]*>([^<]*)</d:\1>")
PATH = "/resource-center/data-chart-center/interest-rates/pages/xml"


@dataclass
class Curve:
    date: float                     # unix seconds of the business day
    rates: dict[str, float]         # BC_10YEAR -> 4.79 (percent)

    def get(self, field: str) -> float | None:
        return self.rates.get(field)


def parse(xml: str) -> list[Curve]:
    """Every day in the file, oldest first. Regex, not an XML parser: the shape is flat and the file can be large."""
    out = []
    for body in _ENTRY.findall(xml):
        props = dict(_PROP.findall(body))
        day = props.get("NEW_DATE") or props.get("INDEX_DATE")
        if not day:
            continue
        rates = {}
        for k, v in props.items():
            if k not in ("NEW_DATE", "INDEX_DATE", "Id") and v.strip():
                try:
                    rates[k] = float(v)
                except ValueError:
                    pass
        out.append(Curve(datetime.strptime(day[:10], "%Y-%m-%d").replace(tzinfo=UTC).timestamp(), rates))
    return sorted(out, key=lambda c: c.date)


class Treasury(Source):
    def __init__(self) -> None:
        super().__init__(name="treasury.gov", base_url="https://home.treasury.gov", delay=DAILY, rate=0.2, burst=2, timeout=45.0, retries=1,
                         serves="the official daily par yield curve, real yields and bill rates")

    async def curves(self, data: str = "daily_treasury_yield_curve", year: int | None = None, month: str = "") -> tuple[list[Curve], Provenance]:
        """`month` is YYYYMM for a small file; `year` for a whole year (the curve a year ago)."""
        params = {"data": data}
        if month:
            params["field_tdr_date_value_month"] = month
        else:
            params["field_tdr_date_value"] = str(year or datetime.now(UTC).year)
        xml, at = await self.get(PATH, params, ttl=3 * 3600, persist=True, text=True)
        days = parse(xml)
        return days, Provenance(self.name, at, days[-1].date if days else at, DAILY)

    async def recent(self, data: str = "daily_treasury_yield_curve") -> tuple[list[Curve], Provenance]:
        """This month, plus last month when this one has fewer than two business days so far."""
        now = datetime.now(UTC)
        days, prov = await self.curves(data, month=now.strftime("%Y%m"))
        if len(days) < 2:
            y, m = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
            older, _ = await self.curves(data, month=f"{y}{m:02d}")
            days = older + days
        return days, prov
