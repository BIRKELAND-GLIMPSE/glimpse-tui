"""Deribit's public API: the DVOL index, the option book summary and the BTC index price. No key, no account.

JSON-RPC over HTTP GET. A good answer is `{"jsonrpc": "2.0", "result": ...}`; a bad one is HTTP 400 with
`{"error": {"code": -32602, ...}}`, which the core turns into a `SourceError`. Timestamps are milliseconds, `mark_iv`
is in percent, and an instrument reads `BTC-<DDMMMYY>-<strike>-<C|P>`. Options expire at 08:00 UTC. Deribit gives no
number for anonymous use, so the terminal stays at one request a second (SOURCES.md). Shapes:
tests/fixtures/sources/deribit/.
"""
from __future__ import annotations

import statistics
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .core import LIVE, Provenance, Source, SourceError

API = "/api/v2/public"
EXPIRY_HOUR_UTC = 8
HOUR_MS = 3_600_000


@dataclass(frozen=True)
class AtmVol:
    """At-the-money implied volatility for one expiry, read off the option book."""
    label: str              # "20SEP26", as Deribit writes it
    expiry: float           # unix seconds, 08:00 UTC on the day
    forward: float          # the expiry's `underlying_price`, the median across its rows
    strike: float           # the listed strike nearest the forward
    iv: float               # percent a year, interpolated in strike to the forward


def parse_instrument(name: str) -> tuple[str, float, float, str] | None:
    """`BTC-20SEP26-79500-C` -> ("20SEP26", expiry in unix seconds, 79500.0, "C"). None for anything else."""
    parts = name.split("-")
    if len(parts) != 4 or parts[3] not in ("C", "P"):
        return None
    try:
        day = datetime.strptime(parts[1].title(), "%d%b%y").replace(tzinfo=UTC, hour=EXPIRY_HOUR_UTC)
        return parts[1], day.timestamp(), float(parts[2]), parts[3]
    except ValueError:
        return None


def atm_vols(rows: list[dict[str, Any]], now: float | None = None, n: int = 4) -> list[AtmVol]:
    """ATM `mark_iv` for the `n` nearest expiries that have not passed. A call and a put of one strike carry the
    same mark volatility, so they are averaged; the two strikes either side of the forward are then interpolated
    to it. With the forward outside the listed strikes the nearest strike is used as it stands."""
    now = time.time() if now is None else now
    by_expiry: dict[tuple[float, str], dict[float, list[float]]] = {}
    forwards: dict[tuple[float, str], list[float]] = {}
    for r in rows:
        hit = parse_instrument(str(r.get("instrument_name", "")))
        iv = r.get("mark_iv")
        if not hit or not iv or hit[1] <= now:
            continue
        label, expiry, strike, _ = hit
        by_expiry.setdefault((expiry, label), {}).setdefault(strike, []).append(float(iv))
        if r.get("underlying_price"):
            forwards.setdefault((expiry, label), []).append(float(r["underlying_price"]))
    out = []
    for (expiry, label), strikes in sorted(by_expiry.items())[:n]:
        if not forwards.get((expiry, label)):
            continue
        fwd = statistics.median(forwards[(expiry, label)])     # rows are marked a few milliseconds apart: take the middle forward
        ivs = {k: sum(v) / len(v) for k, v in strikes.items()}
        below = max((k for k in ivs if k <= fwd), default=None)
        above = min((k for k in ivs if k >= fwd), default=None)
        nearest = min(ivs, key=lambda k: abs(k - fwd))
        if below is None or above is None or below == above:
            iv = ivs[nearest]
        else:
            iv = ivs[below] + (ivs[above] - ivs[below]) * (fwd - below) / (above - below)
        out.append(AtmVol(label, expiry, fwd, nearest, iv))
    return out


def window(days: float, now: float | None = None) -> tuple[int, int]:
    """(start_ms, end_ms) for the last `days`, ending on the next whole hour so a minute of requests shares one cache key."""
    now = time.time() if now is None else now
    end = (int(now * 1000) // HOUR_MS + 1) * HOUR_MS
    return end - int(days * 86_400_000), end


class Deribit(Source):
    def __init__(self) -> None:
        super().__init__(name="deribit", base_url="https://www.deribit.com", delay=LIVE, rate=1.0, burst=2,
                         serves="BTC implied volatility: the DVOL index, option mark volatilities, the index price")

    async def _rpc(self, method: str, params: dict[str, Any], ttl: float, persist: bool = False) -> tuple[Any, float]:
        v, at = await self.get(f"{API}/{method}", params, ttl=ttl, persist=persist)
        if not isinstance(v, dict) or "result" not in v:
            err = (v or {}).get("error", {}) if isinstance(v, dict) else {}
            raise SourceError(f"deribit: {err.get('message', 'no result')} ({err.get('code', '?')})")
        return v["result"], at

    async def index_price(self, index: str = "btc_usd") -> tuple[float, Provenance]:
        r, at = await self._rpc("get_index_price", {"index_name": index}, ttl=10)
        return float(r["index_price"]), self.prov(at)

    async def dvol(self, resolution: str, start_ms: int, end_ms: int, currency: str = "BTC") -> tuple[list[list[float]], Provenance]:
        """Rows `[ts_ms, open, high, low, close]`, oldest first. Both timestamps are required by the API. Resolutions
        that answered in Phase 0: "60", "3600", "43200" and "1D"."""
        r, at = await self._rpc("get_volatility_index_data", {"currency": currency, "resolution": str(resolution),
                                                              "start_timestamp": int(start_ms), "end_timestamp": int(end_ms)}, ttl=300)
        rows = sorted((row for row in r.get("data", []) if len(row) >= 5 and start_ms <= row[0] <= end_ms), key=lambda row: row[0])
        return rows, self.prov(at, as_of=rows[-1][0] / 1000 if rows else None)

    async def book_summary(self, kind: str = "option", currency: str = "BTC") -> tuple[list[dict[str, Any]], Provenance]:
        """One row per instrument: `instrument_name`, `mark_iv` (percent), `underlying_price` (the expiry's forward),
        `open_interest`, `volume_usd`. `high`, `low` and `price_change` can be null. About 434 KB for BTC options."""
        r, at = await self._rpc("get_book_summary_by_currency", {"currency": currency, "kind": kind}, ttl=60)
        return list(r), self.prov(at)

    async def instruments(self, kind: str = "option", currency: str = "BTC") -> tuple[list[dict[str, Any]], Provenance]:
        """The instrument list costs twenty times an ordinary request in Deribit's credit system: cached for an hour."""
        r, at = await self._rpc("get_instruments", {"currency": currency, "kind": kind, "expired": "false"}, ttl=3600, persist=True)
        return list(r), self.prov(at)
