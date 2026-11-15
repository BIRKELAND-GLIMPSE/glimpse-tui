"""A volatility clock that knows when a market trades.

Gold and oil settle on Glimpse every hour, but the markets that move them keep CME's week: Sunday 23:00 to Friday
22:00 UTC with an hour's break at 22:00 each day. In the hours between, their oracles barely move (PAX Gold drifts a
little on Coinbase, WTI/BTC moves only with Bitcoin), yet the hourly ladders through a weekend are priced as if
every hour were a trading hour. Bitcoin trades every hour, but its Saturdays and Sundays move at half the σ of its
weekdays (measured 31 October 2026: 0.22% an hour against 0.43%; gold 0.07% against 0.26%, WTI/BTC 0.30% against
0.73%). This clock measures an asset's hourly move separately for its busy hours and its quiet hours, CME's closed
hours for gold and oil and the weekend for everything else, and adds up the variance to a close hour by hour at the
right σ. `zoo.core.Scale`, the clock every model in the zoo sizes from, uses the same calendar through `quiet_at`
and `quiet_ratio`, so every picture narrows through a weekend.

`Clock.fit` reads the open-hour σ as an exponentially weighted average of recent open hours (so it follows the
volatility regime), the closed-hour σ as the long-run ratio of closed to open hours times that, and the Student-t ν
of one open hour's standardised move. `Clock.picture` is the Student-t of the log price at a close around spot with
the clock's σ, integrated between a ladder's edges. The `session_walk` model in the zoo is this picture.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np

SESSIONS = {"XAU": "cme", "PAXG": "cme", "XAUT": "cme", "XAUTBTC": "cme", "WTI": "cme", "WTIBTC": "cme"}
QUIET_DEFAULT = {"cme": 0.3, "always": 1.0}   # a quiet hour's σ over a busy hour's, before a hundred quiet hours are read
HALFLIFE_H = 120                # open hours the EWMA of the open-hour variance looks back
HISTORY = 1000                  # hours of each kind the fit reads at most
SIGMA_DEFAULT = 0.005           # an hour's σ before there are a hundred hours to read
SIGMA_FLOOR = 2e-4


def cme_open(t: float) -> bool:
    """Whether CME's gold and oil futures trade at unix time `t`: Sunday 23:00 to Friday 22:00 UTC with the 22:00 hour
    off each day (an hour either way across the clock change; the measured σ of the hours called closed absorbs it)."""
    d = datetime.fromtimestamp(t, UTC)
    wd, h = d.weekday(), d.hour
    if wd == 5:
        return False
    if wd == 6:
        return h >= 23
    if wd == 4:
        return h < 22
    return h != 22


def weekend(t: float) -> bool:
    """Saturday or Sunday, UTC: the quiet hours of a market that never closes."""
    return datetime.fromtimestamp(t, UTC).weekday() >= 5


def session_of(asset: str) -> str:
    """Which calendar an asset's market keeps: "cme" for gold and oil in any currency, "always" for the rest (open every
    hour, quiet at the weekend)."""
    return SESSIONS.get(str(asset).upper(), "always")


def is_open(session: str, t: float) -> bool:
    """A busy hour: one the market trades (cme), or a weekday hour (always). The "still" session, a series with no hourly
    moves to measure (`zoo.core.Scale.still`), keeps no calendar: every hour is busy."""
    if session == "still":
        return True
    return cme_open(t) if session == "cme" else not weekend(t)


def quiet_at(session: str, t: float) -> bool:
    return not is_open(session, t)


def quiet_ratio(r_busy, r_quiet, session: str = "always") -> float:
    """A quiet hour's σ over a busy hour's, from two sets of hourly returns: the measured ratio once there are a hundred
    quiet hours, never above 1, and 1 when the lull is under 10% (noise, not a calendar)."""
    r_busy, r_quiet = np.asarray(r_busy, dtype=float), np.asarray(r_quiet, dtype=float)
    if r_quiet.size < 100 or r_busy.size < 100:
        return QUIET_DEFAULT.get(session, 1.0)
    sb = float(np.std(r_busy))
    if not sb > 0:
        return 1.0
    ratio = float(np.clip(np.std(r_quiet) / sb, 0.05, 1.0))
    return 1.0 if ratio > 0.9 else ratio


@dataclass(frozen=True)
class Clock:
    """An asset's hourly move by whether its market is busy: σ of the hourly log return in open (weekday) hours and in
    closed (weekend) hours, the Student-t ν of a busy hour's standardised move, and the calendar that says which is which."""
    s_open: float
    s_closed: float
    nu: float
    session: str = "always"

    @classmethod
    def fit(cls, bars, session: str = "always", halflife: float = HALFLIFE_H) -> Clock:
        """From hourly bars (a `close` column indexed by the bar's open time, UTC)."""
        from .zoo.core import fit_student_df

        logp = np.log(bars["close"].to_numpy(dtype=float))
        r = np.diff(logp)
        when = bars.index[1:].as_unit("s").asi8.astype(float)
        op = np.array([is_open(session, t) for t in when], dtype=bool)
        ok = np.isfinite(r)
        ro, rc = r[ok & op][-HISTORY:], r[ok & ~op][-HISTORY:]
        if ro.size < 100:
            s_open, long_open = SIGMA_DEFAULT, SIGMA_DEFAULT
        else:
            w = 0.5 ** (np.arange(ro.size)[::-1] / halflife)
            s_open = float(math.sqrt((w * ro * ro).sum() / w.sum()))
            long_open = float(np.std(ro))
        s_open = max(s_open, SIGMA_FLOOR)
        s_closed = max(s_open * quiet_ratio(ro, rc, session), SIGMA_FLOOR)
        nu = float(fit_student_df(ro / max(long_open, 1e-12))) if ro.size >= 200 else 5.0
        return cls(s_open, s_closed, max(nu, 2.5), session)

    def open_at(self, t: float) -> bool:
        return is_open(self.session, t)

    def steps(self, now: float, end: float) -> list[tuple[float, float]]:
        """The hourly steps from `now` to `end`, partial first and last: (seconds, σ of a whole hour)."""
        if end <= now:
            return [(1e-3, self.s_closed)]
        bounds = np.concatenate([[now], np.arange((now // 3600 + 1) * 3600, end, 3600.0), [end]])
        bounds = bounds[np.concatenate([[True], np.diff(bounds) > 1e-6])]
        return [(float(b - a), self.s_open if self.open_at(a) else self.s_closed) for a, b in zip(bounds[:-1], bounds[1:], strict=True)]

    def variance_to(self, now: float, end: float) -> float:
        return sum(s * s * dt / 3600.0 for dt, s in self.steps(now, end))

    def sigma_to(self, now: float, end: float) -> float:
        return float(math.sqrt(max(self.variance_to(now, end), 1e-12)))

    def hours_open(self, now: float, end: float) -> float:
        """How many of the hours to the close the market trades."""
        return sum(dt / 3600.0 for dt, s in self.steps(now, end) if s == self.s_open) if end > now else 0.0

    @property
    def unit(self) -> float:
        """What divides a t_ν draw to unit variance."""
        return math.sqrt(self.nu / (self.nu - 2)) if self.nu > 2 else 1.0

    def cdf(self, spot: float, sigma: float, prices) -> np.ndarray:
        """The chance the close is under each price: a Student-t of the log price around `spot` with scale `sigma`."""
        from scipy.stats import t as student

        x = (np.log(np.maximum(np.asarray(prices, dtype=float), 1e-12)) - math.log(spot)) / max(sigma, 1e-9) * self.unit
        return student.cdf(x, self.nu)

    def picture(self, spot: float, sigma: float, edges) -> np.ndarray:
        """One probability per range of a ladder with `edges`; what falls past the ends goes in the end ranges."""
        c = self.cdf(spot, sigma, edges)
        c[0], c[-1] = 0.0, 1.0
        p = np.maximum(np.diff(c), 1e-12)
        return p / p.sum()

    def describe(self, now: float, end: float) -> str:
        """`σ 1.2% to the close · 0.39%/h open, 0.12%/h closed · ν 3.0 · closed now, 7.5 h to go, 2.0 of them open`."""
        sigma = self.sigma_to(now, end)
        hours = max((end - now) / 3600.0, 1e-3)
        if self.session == "always":
            return (f"σ {sigma:.1%} to the close · {self.s_open:.2%}/h weekdays, {self.s_closed:.2%}/h weekends · ν {self.nu:.1f} · "
                    f"{'weekday' if self.open_at(now) else 'weekend'} now, {hours:.1f} h to go, {self.hours_open(now, end):.1f} of them weekday")
        return (f"σ {sigma:.1%} to the close · {self.s_open:.2%}/h CME open, {self.s_closed:.2%}/h closed · ν {self.nu:.1f} · "
                f"{'open' if self.open_at(now) else 'closed'} now, {hours:.1f} h to go, {self.hours_open(now, end):.1f} of them open")
