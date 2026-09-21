"""Number formatting, matching the web app's conventions. Sats on screen, UTC everywhere."""
from __future__ import annotations

import time
from datetime import UTC, datetime

MINUS = "−"


def sats(n: float, signed: bool = False) -> str:
    r = round(n)
    sign = MINUS if r < 0 else "+" if signed and r > 0 else ""
    return f"{sign}₿{abs(r):,}"


SATS = 100_000_000


def in_btc(n: float | None, signed: bool = False) -> str:
    """A price in Bitcoin. Satoshis up to a whole coin (`₿3,712,400`), bitcoin above it (`₿7.0123`), and
    fractions of a sat below one, because a euro is about 900 sats and a share of a penny stock is fewer."""
    if n is None:
        return "–"
    sign = MINUS if n < 0 else "+" if signed and n > 0 else ""
    a = abs(n)
    if a >= SATS:
        return f"{sign}₿{a / SATS:,.4f}"
    if a >= 1000:
        return f"{sign}₿{a:,.0f}"
    return f"{sign}₿{a:,.2f}" if a >= 1 else f"{sign}₿{a:,.4f}"


def price(x: float) -> str:
    return f"{x:,.0f}" if x >= 100 else f"{x:,.2f}"


def kprice(x: float) -> str:
    return f"{x / 1000:,.0f}k" if x >= 10_000 else price(x)


def span(lo: float, hi: float) -> str:
    return f"{price(lo)}–{price(hi)}"


def odds(x: float) -> str:
    if x <= 0:
        return "-"
    if x >= 100:
        return f"{x:,.0f}×"
    return f"{x:.1f}×" if x >= 10 else f"{x:.2f}×"


def pct(p: float) -> str:
    """Probability 0..1."""
    v = p * 100
    if v < 0.01:
        return "<0.01%"
    return f"{v:.2f}%" if v < 1 else f"{v:.1f}%"


def roi(frac: float) -> str:
    v = abs(frac) * 100
    body = f"{v:,.0f}" if v >= 100 else f"{v:.1f}"
    return f"{MINUS if frac < 0 else '+'}{body}%"


def contracts(n: float) -> str:
    return f"{n:,.0f}" if n >= 100 or n == int(n) else f"{n:.2f}"


def close_label(end_utc: int, hourly: bool) -> str:
    """The site labels a close at end-1ms so a 00:00 close reads as the prior day; the terminal
    shows the true UTC instant instead, which is what a bot trades against."""
    d = datetime.fromtimestamp(end_utc, UTC)
    return d.strftime("%d %b %H:%M") if hourly else d.strftime("%a %d %b")


def question(asset: str, end_utc: int) -> str:
    """What a market asks, the way a trader says it: BTC at 18 Sep 11:00 UTC."""
    return f"{asset} at {datetime.fromtimestamp(end_utc, UTC).strftime('%d %b %H:%M')} UTC"


def countdown(end_utc: int, now: float | None = None) -> str:
    s = int(end_utc - (time.time() if now is None else now))
    if s <= 0:
        return "closed"
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m, sec = divmod(rem, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m {sec:02d}s"


def clock() -> str:
    return datetime.now(UTC).strftime("%H:%M:%S UTC")


def bar(frac: float, width: int) -> str:
    """Horizontal bar with eighth-block resolution."""
    frac = max(0.0, min(1.0, frac))
    eighths = round(frac * width * 8)
    full, part = divmod(eighths, 8)
    return "█" * full + (" ▏▎▍▌▋▊▉"[part] if part else "")
