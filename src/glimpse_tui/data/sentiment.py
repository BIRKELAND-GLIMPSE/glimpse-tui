"""The Crypto Fear and Greed Index from alternative.me: one number a day, 0 (extreme fear) to 100 (extreme greed).

Their terms: "You must properly acknowledge the source of the data and prominently reference it accordingly.
Commercial use is allowed as long as the attribution is given right next to the display of the data." So the
source's `note` carries the attribution, and any pane that shows the number prints `source: alternative.me` beside it.
All values arrive as strings, newest first; only the first row has `time_until_update`, in seconds.
"""
from __future__ import annotations

from dataclasses import dataclass

from .core import DAILY, Provenance, Source, SourceError

ATTRIBUTION = "source: alternative.me"


@dataclass(frozen=True)
class Reading:
    date: float             # unix seconds, midnight UTC of the day described
    value: int              # 0 to 100
    label: str              # "Extreme Fear" … "Extreme Greed", in their words


def parse(doc: dict) -> tuple[list[Reading], float]:
    """(readings oldest first, seconds until the next update)."""
    out = []
    for row in doc.get("data", []):
        try:
            out.append(Reading(float(row["timestamp"]), int(row["value"]), str(row.get("value_classification", ""))))
        except (KeyError, TypeError, ValueError):
            continue
    try:
        wait = float(doc["data"][0].get("time_until_update", 3600))
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        wait = 3600.0
    return sorted(out, key=lambda r: r.date), wait


class FearGreed(Source):
    def __init__(self) -> None:
        super().__init__(name="alternative.me", base_url="https://api.alternative.me", delay=DAILY, rate=0.2, burst=2, note=ATTRIBUTION,
                         serves="the Crypto Fear and Greed Index, daily (attribution shown beside the number)")

    async def index(self, limit: int = 30) -> tuple[list[Reading], Provenance]:
        """The last `limit` days, oldest first. Polled no more often than once an hour; the index changes once a day."""
        doc, at = await self.get("/fng/", {"limit": limit}, ttl=3600, persist=True)
        rows, _ = parse(doc if isinstance(doc, dict) else {})
        if not rows:
            raise SourceError("alternative.me: no readings in the answer")
        return rows, self.prov(at, as_of=rows[-1].date)
