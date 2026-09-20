"""The monitor strip under the header: the markets that matter, then world clocks and exchange session states.

Pure rendering from the hub's cached state. The header above it is the same one every screen shows (chrome.py).
"""
from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from rich.text import Text

from ..theme import DIM, FAINT, GREEN, RED, TEXT
from . import ui

if TYPE_CHECKING:
    from .hub import Hub

BAR = "#161616"
INK = "#0D0D0D"
LABEL = {"XAU": "GOLD", "BRENT": "OIL", "US10Y": "10Y", "US2Y": "2Y", "SPX": "S&P", "NDX": "NDX"}   # plain names on the strip
CITY = {"UTC": "UTC", "America/New_York": "NY", "Europe/London": "LDN", "Asia/Tokyo": "TYO", "Asia/Hong_Kong": "HK",
        "Asia/Singapore": "SG", "Europe/Zurich": "ZRH", "Europe/Berlin": "BER", "Australia/Sydney": "SYD", "America/Chicago": "CHI"}

# (name, zone, open, close, weekdays) in exchange-local time. Holidays are not modelled; the label says "hours".
SESSIONS = (("NYSE", "America/New_York", (9, 30), (16, 0), range(0, 5)),
            ("CME", "America/Chicago", (17, 0), (16, 0), range(0, 7)),          # Sunday 17:00 to Friday 16:00, daily break 16:00-17:00
            ("TSE", "Asia/Tokyo", (9, 0), (15, 30), range(0, 5)))


def clock(zone: str, now: datetime | None = None) -> tuple[str, str]:
    now = now or datetime.now(UTC)
    try:
        local = now.astimezone(ZoneInfo(zone))
    except Exception:
        local = now
    return CITY.get(zone, zone.split("/")[-1][:3].upper()), local.strftime("%H:%M")


def session_open(name: str, now: datetime | None = None) -> bool:
    """Regular trading hours by the clock. CME Globex runs Sunday 17:00 to Friday 16:00 Chicago with an hour off a day."""
    now = now or datetime.now(UTC)
    for n, zone, op, cl, days in SESSIONS:
        if n != name:
            continue
        local = now.astimezone(ZoneInfo(zone))
        minutes, o, c = local.hour * 60 + local.minute, op[0] * 60 + op[1], cl[0] * 60 + cl[1]
        if name == "CME":
            wd = local.weekday()
            if wd == 5 or (wd == 6 and minutes < o) or (wd == 4 and minutes >= c):
                return False
            return not (c <= minutes < o)
        return local.weekday() in days and o <= minutes < c
    return False


def instrument_open(session: str, now: datetime | None = None) -> bool:
    """Is a quote for an instrument with this session expected to move right now?"""
    now = now or datetime.now(UTC)
    if session == "24x7":
        return True
    if session == "us_equity":
        return session_open("NYSE", now)
    if session == "24x5":                               # FX and metals: Sunday 17:00 to Friday 17:00 New York
        local = now.astimezone(ZoneInfo("America/New_York"))
        wd, m = local.weekday(), local.hour * 60 + local.minute
        return not (wd == 5 or (wd == 6 and m < 17 * 60) or (wd == 4 and m >= 17 * 60))
    return False                                        # daily series never tick


def next_open(session: str, now: datetime | None = None) -> datetime | None:
    now = now or datetime.now(UTC)
    probe = now.replace(second=0, microsecond=0)
    for _ in range(8 * 24 * 4):
        probe += timedelta(minutes=15)
        if instrument_open(session, probe):
            return probe
    return None


def clocks(hub: Hub, width: int, now: float) -> Text:
    """`NY 16:32  LDN 21:32  TYO 05:32 · NYSE shut`: the world's clocks, and whether the big exchanges are open."""
    out = Text(no_wrap=True)
    utc_now = datetime.fromtimestamp(now, UTC)
    zones = [z for z in hub.cfg.get("clocks", ["UTC", "America/New_York"]) if z != "UTC"]
    for z in zones if width >= 190 else zones[:2]:
        city, hhmm = clock(z, utc_now)
        out.append(f"{city} ", style=FAINT)
        out.append(f"{hhmm}  ", style=f"bold {TEXT}")
    if width >= 210:
        for name in ("NYSE", "CME", "TSE"):
            on = session_open(name, utc_now)
            out.append(f"{name} ", style=FAINT)
            out.append("open  " if on else "shut  ", style=GREEN if on else DIM)
    return out


def strip(hub: Hub, width: int) -> Text:
    """The monitor line under the header: every instrument in the tape list with its change, then the clocks.
    A narrow terminal keeps the markets and drops the clocks."""
    now = time.time()
    right = clocks(hub, width, now)
    left = tape(hub, width - (right.cell_len + 2 if width >= 120 else 0))
    if width < 120 or left.cell_len + right.cell_len + 1 > width:
        right = Text("")
    out = Text(no_wrap=True, overflow="crop")
    out.append_text(left)
    out.append(" " * max(width - left.cell_len - right.cell_len, 1))
    out.append_text(right)
    out.stylize_before(f"on {BAR}")
    return out


def tape(hub: Hub, width: int) -> Text:
    """Every instrument in the tape list, with its change. `MEMP` is the mempool's size, for those who add it."""
    now = time.time()
    out = Text(no_wrap=True, overflow="crop")
    out.append(" ")
    for name in hub.cfg.get("tape", []):
        name = name.upper()
        part = Text(no_wrap=True)
        if name == "MEMP":
            if not hub.chain.mempool_vsize:
                continue
            part.append("MEMP ", style=FAINT)
            part.append(f"{hub.chain.mempool_vsize / 1e6:,.0f} MvB", style=f"bold {TEXT}")
        else:
            if not (q := hub.quotes.get(name)):
                continue
            ins = hub.book.get(name)
            stale = q.prov.stale(now)
            part.append(f"{LABEL.get(name, name)} ", style=FAINT)
            pct_unit = bool(ins and ins.cls == "GOVT")
            decimals = 0 if q.price >= 1_000 else ins.decimals if ins else 2           # a glance, not a quote screen
            part.append(ui.px(q.price, decimals) + ("%" if pct_unit else ""), style=f"bold {DIM if stale else TEXT}")
            if q.prev:
                part.append(" ")
                part.append_text(ui.signed(q.change, 2) if pct_unit else ui.signed_pct(q.pct))
            if q.proxy_for:
                part.append(f" via {q.via or 'proxy'}", style=FAINT)     # a stand-in is quoted: gold is PAXG until a spot feed exists
            if q.prov.delay in ("daily", "weekly", "monthly"):
                part.append(f" {q.prov.delay[0]}", style=FAINT)      # d for a daily close
        if out.cell_len + part.cell_len + 2 > width:
            break
        out.append_text(part)
        out.append("  ")
    if out.cell_len <= 1:
        out.append("markets fill in as their sources answer · SRC shows every source", style=FAINT)
    return out


def colour_of(change: float | None) -> str:
    return GREEN if change and change > 0 else RED if change and change < 0 else DIM
