"""The status bar and the ticker tape (TERMINAL.md 3.3), plus world clocks and exchange session states.

Pure rendering from the hub's cached state: brand, BTC and its change, the fastest fee, the tip and its age, the
Glimpse balance, then clocks. The second line is the tape. A new block makes the height flash for two seconds.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from rich.text import Text

from .. import fmt
from ..data.core import ago
from ..theme import DIM, FAINT, GREEN, ORANGE, RED, TEXT
from . import ui

if TYPE_CHECKING:
    from .hub import Hub

BAR = "#161616"
INK = "#0D0D0D"
FLASH_S = 2.0
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


def status_bar(hub: Hub, width: int, balance: float | None = None, authenticated: bool = False) -> Text:
    now = time.time()
    left = Text(no_wrap=True)
    left.append(" GLIMPSE ", style=f"bold {INK} on {ORANGE}")
    left.append(" TERMINAL ", style=f"bold {ORANGE} on #2b1500")
    q = hub.quotes.get("BTC")
    if q:
        stale = q.prov.stale(now)
        left.append("  BTC ", style=FAINT)
        left.append(ui.px(q.price, 0), style=f"bold {DIM if stale else TEXT}")
        left.append(" ")
        left.append_text(ui.signed_pct(q.pct))
    c = hub.chain
    if c.fastest_fee:
        left.append("  FEE ", style=FAINT)
        left.append(f"{c.fastest_fee:.0f}" if c.fastest_fee >= 10 or c.fastest_fee == int(c.fastest_fee) else f"{c.fastest_fee:.1f}",
                    style=f"bold {TEXT}")
        left.append(" s/vB", style=FAINT)
    if c.height:
        flashing = now - c.block_seen_at < FLASH_S
        left.append("  ")
        left.append(f"#{c.height:,}", style=f"bold {INK} on {ORANGE}" if flashing else f"bold {TEXT}")
        if c.tip_time:
            left.append(f" {ago(now - c.tip_time)}", style=FAINT)

    right = Text(no_wrap=True)
    if balance is not None:
        right.append(fmt.sats(balance), style=f"bold {ORANGE}")
        right.append("   ")
    elif not authenticated:
        right.append("READ-ONLY", style=f"bold {DIM}")
        right.append("   ")
    zones = hub.cfg.get("clocks", ["UTC", "America/New_York"])
    shown = zones if width >= 150 else zones[:2]
    utc_now = datetime.fromtimestamp(now, UTC)
    for z in shown:
        city, hhmm = clock(z, utc_now)
        right.append(f"{city} ", style=FAINT)
        right.append(f"{hhmm}  ", style=f"bold {TEXT}")
    if width >= 170:
        for name in ("NYSE", "CME", "TSE"):
            on = session_open(name, utc_now)
            right.append(f"{name} ", style=FAINT)
            right.append("open  " if on else "shut  ", style=GREEN if on else DIM)
    while left.cell_len + right.cell_len + 1 > width and right.cell_len:
        right = Text("")                                # a narrow terminal keeps the market side
    out = Text(no_wrap=True, overflow="crop")
    out.append_text(left)
    out.append(" " * max(width - left.cell_len - right.cell_len, 1))
    out.append_text(right)
    out.stylize_before(f"on {BAR}")
    return out


def tape(hub: Hub, width: int) -> Text:
    """The second line: every instrument in the tape list, with its change. `MEMP` is the mempool's size."""
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
            if name == "BTC" or not (q := hub.quotes.get(name)):
                continue                                # BTC already leads the status bar
            ins = hub.book.get(name)
            stale = q.prov.stale(now)
            part.append(f"{name} ", style=FAINT)
            pct_unit = bool(ins and ins.cls == "GOVT")
            part.append(ui.px(q.price, ins.decimals if ins else 2) + ("%" if pct_unit else ""), style=f"bold {DIM if stale else TEXT}")
            if q.proxy_for:
                part.append(" proxy", style=FAINT)
            if q.prev:
                part.append(" ")
                part.append_text(ui.signed(q.change, 2) if pct_unit else ui.signed_pct(q.pct))
            if q.prov.delay != "live":
                part.append(f" {q.prov.delay[0]}", style=FAINT)      # d for a daily close
        if out.cell_len + part.cell_len + 2 > width:
            break
        out.append_text(part)
        out.append("  ")
    if out.cell_len <= 1:
        out.append("the tape fills as sources answer · SRC shows them", style=FAINT)
    return out


def colour_of(change: float | None) -> str:
    return GREEN if change and change > 0 else RED if change and change < 0 else DIM
