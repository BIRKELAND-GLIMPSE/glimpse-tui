"""Calendar and cycle pictures: the lab's family "Calendar & cycles" (`models/zoo_calendar.py`).

Each model is one causal signal read off the hourly bars (the value at bar t uses bar t and earlier bars, or the
calendar alone) with the sign the idea claims. As in the lab, a calendar signal looks ahead from the last complete
bar (the next 12 or 24 hours), not from the market's close. `conviction_picture` turns the latest reading into a
lean on the family's clock: it lands within half a day and does not compound.

The macro calendar lives here as a constant because the terminal has no yaml; `volatility.py` imports it.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from scipy.linalg import eigh as scipy_eigh

from .core import Ctx, Model, cached, conviction_picture
from .signals import hourly_vol, log_close

FAMILY = "Calendar & cycles"
FACTORS = ("direction", "timing")
BOOK = "Kakushadze and Serur, 151 Trading Strategies (2018)"

# Scheduled US macro releases, copied from the lab's `config/events.yaml` (generated there from
# docs/reference/verification/event_calendar_2026.yaml). Local America/New_York dates; FOMC decisions are at 14:00,
# CPI and payrolls (BLS Employment Situation) at 08:30. Covers FOMC 2026-01-28 to 2027-12-08 (2027 dates are
# tentative) and CPI and payrolls January to December 2026. Outside that span the event models keep the baseline.
EVENT_TIME_LOCAL = {"fomc": "14:00", "cpi": "08:30", "payrolls": "08:30"}
EVENT_DATES: dict[str, tuple[str, ...]] = {
    "fomc": ("2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17", "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
             "2027-01-27", "2027-03-17", "2027-04-28", "2027-06-09", "2027-07-28", "2027-09-15", "2027-10-27", "2027-12-08"),
    "cpi": ("2026-01-13", "2026-02-13", "2026-03-11", "2026-04-10", "2026-05-12", "2026-06-10", "2026-07-14", "2026-08-12",
            "2026-09-11", "2026-10-14", "2026-11-10", "2026-12-10"),
    "payrolls": ("2026-01-09", "2026-02-11", "2026-03-06", "2026-04-03", "2026-05-08", "2026-06-05", "2026-07-02", "2026-08-07",
                 "2026-09-04", "2026-10-02", "2026-11-06", "2026-12-04"),
}


# ── the macro calendar ──────────────────────────────────────

@lru_cache(maxsize=1)
def macro_events() -> dict[str, tuple[datetime, ...]]:
    """Release times in UTC, by kind."""
    ny = ZoneInfo("America/New_York")
    return {kind: tuple(sorted(datetime.fromisoformat(f"{d}T{EVENT_TIME_LOCAL[kind]}").replace(tzinfo=ny).astimezone(UTC) for d in dates))
            for kind, dates in EVENT_DATES.items()}


def hours_to_next_event(index: pd.DatetimeIndex, kinds: tuple[str, ...] = ("fomc", "cpi", "payrolls")) -> pd.Series:
    """Hours from each bar's close to the next scheduled release of the given kinds (NaN when none is listed)."""
    ev = sorted(t for k in kinds for t in macro_events().get(k, ()))
    close = pd.DatetimeIndex(index) + pd.Timedelta(hours=1)
    if not ev:
        return pd.Series(np.nan, index=index)
    evs = pd.DatetimeIndex(ev)
    pos = np.asarray(evs.searchsorted(close, side="left"))
    out = np.full(len(close), np.nan)
    ok = pos < len(evs)
    out[ok] = np.asarray((evs[pos[ok]] - close[ok]).total_seconds()) / 3600.0
    return pd.Series(out, index=index)


def calendar_span(kinds: tuple[str, ...]) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    """The span in which every one of `kinds` is listed: from 45 days before the first listed release to the earliest
    of the kinds' last releases. The lab ends the span at the last release of any kind; the terminal ends it sooner so
    that a kind whose list has run out is never read as 'nothing scheduled'."""
    lists = [macro_events().get(k, ()) for k in kinds]
    if not lists or any(not ev for ev in lists):
        return None
    return pd.Timestamp(min(ev[0] for ev in lists)) - pd.Timedelta(days=45), pd.Timestamp(min(ev[-1] for ev in lists))


def calendar_covers(index: pd.DatetimeIndex, kinds: tuple[str, ...]) -> pd.Series:
    """True for bars inside the span the calendar lists (so a missing event is not read as 'no event')."""
    span = calendar_span(kinds)
    if span is None:
        return pd.Series(False, index=index)
    idx = pd.DatetimeIndex(index)
    return pd.Series(np.asarray((idx >= span[0]) & (idx <= span[1])), index=index)


def covers_now(ctx: Ctx, kinds: tuple[str, ...]) -> bool:
    span = calendar_span(kinds)
    return span is not None and span[0] <= pd.Timestamp(ctx.now, unit="s", tz="UTC") <= span[1]


def window_picture(ctx: Ctx, signal: pd.Series, **kw: object) -> np.ndarray:
    """The picture of a signal that is zero outside a calendar window. A percentile rank would read 'outside the
    window' as the lowest reading ever and lean hard the other way, so outside the window the baseline is kept."""
    last = float(signal.iloc[-1]) if len(signal) else float("nan")
    return conviction_picture(ctx, signal if np.isfinite(last) and last > 0 else None, **kw)


# ── helpers (pure numpy, causal by construction) ────────────

def expanding_group_means(values: np.ndarray, groups: np.ndarray, n_groups: int, min_count: int) -> np.ndarray:
    """(n, n_groups): at row t, the mean of the finite `values[0..t]` whose group is g; NaN below `min_count`.
    Rows with a negative group or a non-finite value add nothing, so a statistic can be posted only on the rows
    where it becomes known (a daily return on the day's last bar)."""
    n = values.size
    ok = np.isfinite(values) & (groups >= 0)
    rows = np.nonzero(ok)[0]
    sums = np.zeros((n, n_groups))
    counts = np.zeros((n, n_groups))
    sums[rows, groups[rows]] = values[rows]
    counts[rows, groups[rows]] = 1.0
    sums = np.cumsum(sums, axis=0)
    counts = np.cumsum(counts, axis=0)
    return np.where(counts >= min_count, sums / np.maximum(counts, 1.0), np.nan)


_STEP_MEMO: dict[tuple, float] = {}


def stepped(bars: pd.DataFrame, name: str, fn: Callable[[np.ndarray], float], *, step: int, lookback: int) -> pd.Series:
    """The lab's `stepped`: a slow signal evaluated every `step` hours (on a fixed UTC grid) from the `lookback` log
    closes ending at each origin and forward-filled to the bars in between. Origins are remembered between bars (keyed
    by the window's time and end prices), so a new bar costs at most one evaluation."""
    idx = pd.DatetimeIndex(bars.index)
    hours = np.asarray(idx.as_unit("s").asi8) // 3600
    lp = log_close(bars).to_numpy(dtype=float)
    out = np.full(len(idx), np.nan)
    for i in np.nonzero((hours % step == 0) & (np.arange(len(idx)) >= lookback - 1))[0]:
        key = (name, lookback, int(hours[i]), float(lp[i - lookback + 1]), float(lp[i]))
        v = _STEP_MEMO.get(key)
        if v is None:
            try:
                v = float(fn(lp[i - lookback + 1 : i + 1]))
            except (ValueError, np.linalg.LinAlgError, FloatingPointError):      # a failed fit at one origin is a missing value
                v = float("nan")
            _STEP_MEMO[key] = v
        out[i] = v
    if len(_STEP_MEMO) > 40_000:
        _STEP_MEMO.clear()
    return pd.Series(out, index=idx).ffill(limit=step - 1)


def dominant_cycle(y: np.ndarray, min_period: float = 12.0, max_period: float = 240.0, ahead: int = 24, pad: int = 4) -> dict[str, float]:
    """Dominant cycle of a series: linear detrend, Hann-windowed periodogram (zero-padded `pad` times) restricted to
    periods in [min_period, max_period] hours, then a least-squares sinusoid at that frequency fitted jointly with
    the trend. Returns the period, amplitude, the sinusoid's change over the next `ahead` steps, and that change in
    units of the series' step volatility times sqrt(ahead).

    The peak is taken against a random walk's spectrum: a random walk's periodogram falls like 1 / (4 sin^2(pi f)),
    so the raw peak would almost always sit at the longest period allowed. Multiplying by 4 sin^2(pi f) picks the
    cycle that stands out most from what a random walk would show at that frequency."""
    y = np.asarray(y, dtype=float)
    if not np.all(np.isfinite(y)):
        raise ValueError("gap in the window")
    n = y.size
    t = np.arange(n, dtype=float)
    trend = np.column_stack([np.ones(n), t])
    coef, *_ = np.linalg.lstsq(trend, y, rcond=None)
    resid = y - trend @ coef
    nfft = pad * n
    power = np.abs(np.fft.rfft(resid * np.hanning(n), nfft)) ** 2
    freqs = np.fft.rfftfreq(nfft)
    power = power * 4.0 * np.sin(np.pi * freqs) ** 2
    band = (freqs >= 1.0 / max_period - 1e-12) & (freqs <= 1.0 / min_period + 1e-12)
    if not band.any():
        raise ValueError("no frequency in the period band")
    f = float(freqs[int(np.argmax(np.where(band, power, -np.inf)))])
    w = 2.0 * np.pi * f
    design = np.column_stack([np.ones(n), t, np.cos(w * t), np.sin(w * t)])
    beta, *_ = np.linalg.lstsq(design, y, rcond=None)
    a, b = float(beta[2]), float(beta[3])
    t_end = n - 1.0
    change = a * (np.cos(w * (t_end + ahead)) - np.cos(w * t_end)) + b * (np.sin(w * (t_end + ahead)) - np.sin(w * t_end))
    vol = float(np.std(np.diff(y))) or 1e-12
    return {"period_hours": 1.0 / f, "amplitude": float(np.hypot(a, b)), "change": float(change),
            "signal": float(change / (vol * np.sqrt(ahead)))}


def ssa_forecast(y: np.ndarray, window: int = 168, components: int = 3, ahead: int = 24) -> dict[str, float]:
    """Basic singular spectrum analysis (Golyandina, Nekrutkin and Zhigljavsky 2001) of a series with its mean
    removed: embed with window L, keep the leading `components` eigentriples of the lag matrix, reconstruct by
    diagonal averaging, and run the recurrent (linear recurrence) forecast `ahead` steps.

    Returns the last reconstructed value, the forecast `ahead` steps later, and the forecast change in units of
    the series' step volatility times sqrt(ahead)."""
    y = np.asarray(y, dtype=float)
    if not np.all(np.isfinite(y)):
        raise ValueError("gap in the window")
    n, L = y.size, int(window)
    K = n - L + 1
    if L < components + 1 or K < L:
        raise ValueError("series too short for the SSA window")
    mu = float(y.mean())
    x = y - mu
    X = np.lib.stride_tricks.sliding_window_view(x, L).T  # (L, K), column j = x[j : j + L]
    _, U = scipy_eigh(X @ X.T, subset_by_index=[L - components, L - 1])  # leading eigenvectors, ascending
    # diagonal averaging, only for the last L values the recurrence needs: position p >= K - 1 averages the
    # entries (i, j) with i + j = p, which all lie in the last L columns of the reconstructed lag matrix
    tail = U @ (U.T @ X[:, K - L :])  # (L, L), column c is lag vector K - L + c
    pos = np.arange(L)[:, None] + np.arange(L)[None, :]
    sums = np.bincount(pos.ravel(), weights=tail.ravel(), minlength=2 * L - 1)
    counts = np.bincount(pos.ravel(), minlength=2 * L - 1)
    recon = (sums / counts)[L - 1 :]  # positions K - 1 .. n - 1
    pi = U[-1, :]
    nu2 = float(pi @ pi)
    if nu2 >= 1.0 - 1e-9:
        raise ValueError("verticality coefficient is 1; the recurrent forecast is undefined")
    coeffs = (U[:-1, :] @ pi) / (1.0 - nu2)  # weights on the previous L - 1 values, oldest first
    buf = list(recon[-(L - 1) :])
    for _ in range(int(ahead)):
        buf.append(float(np.dot(coeffs, buf[-(L - 1) :])))
    vol = float(np.std(np.diff(y))) or 1e-12
    change = buf[-1] - float(recon[-1])
    return {"last": float(recon[-1]) + mu, "forecast": buf[-1] + mu, "change": float(change),
            "signal": float(change / (vol * np.sqrt(ahead)))}


# ── signals ─────────────────────────────────────────────────

def hour_of_day_signal(bars: pd.DataFrame, min_count: int = 30, lead_hours: int = 12) -> pd.Series:
    """Sum of the expanding hour-of-day mean returns over the next `lead_hours` bars' hours, less the same number
    of average hours, divided by the hourly volatility at bar t. The return of bar t is known at its close, so
    the means at bar t include it. (24 lead hours would cover every hour and cancel out.)"""
    r = log_close(bars).diff().to_numpy(dtype=float)
    hours = pd.DatetimeIndex(bars.index).hour.to_numpy()
    means = expanding_group_means(r, hours, 24, min_count)  # (n, 24)
    means = means - means.mean(axis=1, keepdims=True)  # the hour-of-day pattern, not the overall drift
    doubled = np.concatenate([means, means], axis=1)
    csum = np.concatenate([np.zeros((len(r), 1)), np.cumsum(doubled, axis=1)], axis=1)
    rows = np.arange(len(r))
    start = hours + 1  # the next bar's hour
    total = csum[rows, start + lead_hours] - csum[rows, start]
    return pd.Series(total, index=bars.index) / hourly_vol(bars)


def day_of_week_signal(bars: pd.DataFrame, min_count: int = 8) -> pd.Series:
    """The expanding weekday mean of UTC daily returns for the day or days the next 24 hours fall on, relative to
    the average weekday, in units of daily volatility."""
    idx = pd.DatetimeIndex(bars.index)
    lp = log_close(bars).to_numpy(dtype=float)
    n = lp.size
    hours = idx.hour.to_numpy()
    wday = idx.dayofweek.to_numpy()
    # a UTC day's close-to-close return becomes known at the close of its 23:00 bar
    day_ret = np.full(n, np.nan)
    last = np.nonzero(hours == 23)[0]
    last = last[last >= 24]
    day_ret[last] = lp[last] - lp[last - 24]
    groups = np.where(np.isfinite(day_ret), wday, -1)
    means = expanding_group_means(day_ret, groups, 7, min_count)  # (n, 7)
    means = means - means.mean(axis=1, keepdims=True)
    rows = np.arange(n)
    w_today = (23 - hours) / 24.0  # bars t+1 .. end of today
    expected = w_today * means[rows, wday] + (1.0 - w_today) * means[rows, (wday + 1) % 7]
    return pd.Series(expected, index=idx) / (hourly_vol(bars) * np.sqrt(24.0))


def turn_of_month_weight(index: pd.DatetimeIndex, ahead: int = 24) -> pd.Series:
    """Share of the next `ahead` hourly bars (opens t+1 .. t+ahead) whose UTC day is a month's last day or one of
    its first two days. A pure function of the calendar."""
    if len(index) == 0:
        return pd.Series(dtype=float, index=index)
    ext = pd.date_range(index[0], index[-1] + pd.Timedelta(hours=ahead), freq="h")
    inside = np.asarray((ext.day <= 2) | ((ext + pd.Timedelta(days=1)).month != ext.month))
    csum = np.concatenate([[0.0], np.cumsum(inside.astype(float))])
    pos = ext.get_indexer(index)
    return pd.Series((csum[pos + ahead + 1] - csum[pos + 1]) / ahead, index=index)


def pre_fomc_signal(index: pd.DatetimeIndex, window_hours: float = 24.0) -> pd.Series:
    """1 when the next FOMC decision is at most `window_hours` after the bar close, 0 otherwise, NaN outside the span
    the calendar covers."""
    hrs = hours_to_next_event(index, ("fomc",)).to_numpy(dtype=float)
    covered = calendar_covers(index, ("fomc",)).to_numpy(dtype=bool)
    out = np.where(np.isfinite(hrs) & (hrs >= 0) & (hrs <= window_hours), 1.0, 0.0)
    return pd.Series(np.where(covered & np.isfinite(hrs), out, np.nan), index=index)


def fourier_signal(bars: pd.DataFrame, cycle_window: int = 720, min_period: int = 12, max_period: int = 240, step: int = 6) -> pd.Series:
    if len(bars) < cycle_window:
        return pd.Series(np.nan, index=bars.index)
    return stepped(bars, "fourier", lambda y: dominant_cycle(y, float(min_period), float(max_period))["signal"],
                   step=step, lookback=cycle_window)


def ssa_signal(bars: pd.DataFrame, ssa_window_hours: int = 720, embedding: int = 168, components: int = 3, step: int = 6) -> pd.Series:
    if len(bars) < ssa_window_hours or embedding * 2 > ssa_window_hours:
        return pd.Series(np.nan, index=bars.index)
    return stepped(bars, "ssa", lambda y: ssa_forecast(y, embedding, components)["signal"], step=step, lookback=ssa_window_hours)


# ── models ──────────────────────────────────────────────────

def hour_of_day_drift(ctx: Ctx) -> np.ndarray:
    return conviction_picture(ctx, cached(ctx, "hour_of_day_drift:signal", lambda: hour_of_day_signal(ctx.bars)), family=FAMILY)


def day_of_week_drift(ctx: Ctx) -> np.ndarray:
    return conviction_picture(ctx, cached(ctx, "day_of_week_drift:signal", lambda: day_of_week_signal(ctx.bars)), family=FAMILY)


def turn_of_month(ctx: Ctx) -> np.ndarray:
    sig = cached(ctx, "turn_of_month:signal", lambda: turn_of_month_weight(pd.DatetimeIndex(ctx.bars.index)))
    return window_picture(ctx, sig, family=FAMILY)


def pre_fomc_drift(ctx: Ctx) -> np.ndarray:
    if not covers_now(ctx, ("fomc",)):
        return conviction_picture(ctx, None, family=FAMILY)
    sig = cached(ctx, "pre_fomc_drift:signal", lambda: pre_fomc_signal(pd.DatetimeIndex(ctx.bars.index)))
    return window_picture(ctx, sig, family=FAMILY)


def fourier_cycle(ctx: Ctx) -> np.ndarray:
    return conviction_picture(ctx, cached(ctx, "fourier_cycle:signal", lambda: fourier_signal(ctx.bars)), family=FAMILY)


def ssa_trend(ctx: Ctx) -> np.ndarray:
    return conviction_picture(ctx, cached(ctx, "ssa_trend:signal", lambda: ssa_signal(ctx.bars)), family=FAMILY)


def _m(model_id: str, name: str, blurb: str, description: str, fn: Callable[[Ctx], np.ndarray], reference: str, *,
       inputs: str, maths: str, trades: str) -> Model:
    """Every calendar picture goes through `conviction_picture` with sign +1 on the family's clock."""
    return Model(model_id, name, FAMILY, blurb, description, fn, factors=FACTORS, reference=reference,
                 inputs=inputs, maths=maths, trades=trades, pipeline="conviction")


# Shared by every calendar model: the family bends no shape, and the "soon" clock is 1 for closes 6 to 12 hours out
# (a sixth of that one hour out, 0.71 at a day, 0.5 at two days, 0.27 at a week).
_CLOCK = ("family Calendar & cycles: no shape tilt, timing soon, so the drift is at full strength for closes 6 to 12 hours "
          "away, a sixth of it one hour before the close, 0.71 of it at a day and 0.5 at two days.")

MODELS = [
    _m("hour_of_day_drift", "Hour of Day", "Some hours of the day have been stronger than others",
       "Does Bitcoin tend to rise at some hours of the day and fall at others, for example around the US or Asian "
       "open? Trading desks, funding settlements and exchange maintenance all run on a clock, so the average return of "
       "each UTC hour may differ from the rest. For every hour the model keeps a running average of the hourly returns "
       "seen so far, and adds up the averages of the coming 12 hours, relative to an average hour. Hours that have "
       "historically been strong lean the near-term picture bullish, weak hours lean it bearish.",
       hour_of_day_drift, "Intraday seasonality: expanding hour-of-day mean returns (lab implementation)",
       inputs="Hourly closes and the UTC hour of each bar, plus the EWMA hourly volatility from signals.hourly_vol (span 168, "
              "floor 10⁻⁶). It stays silent until every one of the 24 hours has 30 returns (about 720 bars), and the "
              "strength percentile needs 200 readings on top of that (about 920 bars).",
       maths="With r_t = ln C_t − ln C_t−1 and h_t the UTC hour of bar t, m_t(h) is the expanding mean of every r_j with j ≤ t "
             "and h_j = h (bar t's own return included), NaN while any hour has fewer than 30; m̄_t(h) = m_t(h) − (1/24)·Σ_h m_t(h) "
             "removes the overall drift so only the intraday pattern remains. The signal is the sum over j = 1..12 of "
             "m̄_t((h_t + j) mod 24), divided by v_t: the summed pattern of the 12 hours after the last complete bar (24 would "
             "sum to exactly zero) in units of the hourly volatility v_t. It looks 12 hours ahead of the last bar, not to the "
             "market's close. sign = +1 (a positive "
             "reading leans the picture up), " + _CLOCK,
       trades="When the coming hours have historically been strong it puts more weight than the market on the ranges from "
              "about +0.5σ to +2σ above spot and buys those, thins every range below spot and does not buy them; after weak "
              "hours the mirror image. The lean is strongest for closes 6 to 12 hours away and almost gone an hour before "
              "the close. A summed pattern near the middle of its own history leaves the baseline and the bot buys nothing."),
    _m("day_of_week_drift", "Day of Week", "Are Mondays different from Saturdays? Leans with the weekday",
       "The weekday effect: are Mondays different from Saturdays? Weekend books are thin, Monday brings the week's "
       "flows and Friday the position squaring, so each weekday may carry its own average return. For each day of "
       "the week the model keeps a running average of the finished UTC daily returns and reads off the day or days "
       "the next 24 hours fall on, relative to an average weekday. Historically strong weekdays lean the picture "
       "bullish, weak ones bearish.",
       day_of_week_drift, "Day-of-the-week seasonality: expanding weekday mean returns (lab implementation)",
       inputs="Hourly closes with the UTC hour and weekday of each bar, and the EWMA hourly volatility (span 168). A daily return "
              "is posted on the day's 23:00 bar, so it needs eight finished days of every weekday (about 8 weeks, 1,350 bars) "
              "before it reads anything, and 200 readings on top of that for the strength percentile.",
       maths="On each 23:00 UTC bar the day's close-to-close return d = ℓ_t − ℓ_t−24 (ℓ = ln close) is posted to that day's weekday; "
             "m_t(w) is the expanding mean of the posted returns of weekday w up to bar t, NaN while any weekday has fewer than 8, "
             "and m̄_t(w) = m_t(w) − (1/7)·Σ_w m_t(w). With h_t the bar's UTC hour and w_t its weekday, the share of the next 24 "
             "bars still on today's weekday is a = (23 − h_t)/24, and the signal is (a·m̄_t(w_t) + (1 − a)·m̄_t(w_t + 1 mod 7)) / "
             "(v_t·√24): the expected weekday excess over the next 24 hours after the last complete bar, in units of daily "
             "volatility. sign = +1 (a positive reading leans the picture up), " + _CLOCK,
       trades="When the next 24 hours fall on weekdays that have run above the average it puts more weight than the market "
              "on the ranges from about +0.5σ to +2σ above spot and buys those, thinning the ranges below spot; on weak "
              "weekdays the mirror image. Full strength for closes 6 to 12 hours out, half at two days. A reading mid-way "
              "through its own history leaves the baseline and the bot buys nothing."),
    _m("turn_of_month", "Turn of the Month", "Month-end and the first days of a month lift prices",
       "The turn of the month: pay days, fund rebalancing and new monthly allocations have long been said to lift "
       "prices around the last day of a month and the first two days of the next. The model does not fit anything: "
       "it reads the calendar, measures how much of the coming day falls inside that three-day window, and leans "
       "bullish as the window approaches and while it lasts.",
       turn_of_month, "Turn-of-the-month calendar window (lab implementation)",
       inputs="Only the timestamps of the bars: no prices. A reading exists at every bar, so the 400-bar minimum already "
              "gives the 200 readings the strength percentile needs. The bar closest to a month end must be loaded for the "
              "reading to be non-zero.",
       maths="The signal at bar t is the share of the 24 bar opens t+1 .. t+24 whose UTC calendar day is a month's last day or "
             "its 1st or 2nd: 0 outside, ramping up through the day before the month's last day, 1 through the last day and "
             "the 1st, ramping down through the 2nd. Outside the window (reading 0) the picture is the baseline rather than "
             "the percentile's lowest-ever rank. Inside, the strength is the mid-rank percentile among all loaded readings, "
             "most of which are 0, so even a small share ranks above nearly the whole history (about 0.7 with three past "
             "month ends loaded) and a full day inside ranks about 0.9. sign = +1 (a positive reading leans the picture up), "
             + _CLOCK,
       trades="From the day before a month's last day until the 2nd it puts more weight than the market on the ranges from "
              "about +0.5σ to +2σ above spot and buys those; it thins the ranges below spot and never buys them. The lean "
              "is strongest for closes 6 to 12 hours away and fades towards the close and beyond a day. On every other day "
              "of the month the picture is the baseline and the bot buys nothing."),
    _m("pre_fomc_drift", "Pre-FOMC Drift", "Leans bullish in the day before a Fed rate decision",
       "The pre-FOMC drift: in US stocks, much of the equity premium has historically been earned in the 24 hours "
       "before the Federal Reserve announces its rate decision, as uncertainty is priced and then resolved. Bitcoin "
       "trades on the same macro news. The model reads a built-in list of decision dates and leans bullish on the eve "
       "of a decision, saying nothing at any other time.",
       pre_fomc_drift, f"Trading on economic announcements, {BOOK}, Section 19.5; the pre-FOMC announcement drift "
                       "(Lucca and Moench 2015, Journal of Finance)",
       inputs="Only the bar timestamps and the hard-coded FOMC calendar in EVENT_DATES: eight decisions in 2026 (Jan 28, Mar 18, "
              "Apr 29, Jun 17, Jul 29, Sep 16, Oct 28, Dec 9) and eight tentative 2027 dates, at 14:00 New York time (18:00 or "
              "19:00 UTC). Readings exist for every bar from 45 days before the first date to the last date; if the current "
              "time is outside that span the picture is the baseline.",
       maths="For each bar the hours from the bar's close (open + 1 h) to the next listed decision are found; the signal is 1 when "
             "that gap is between 0 and 24 hours, 0 otherwise, NaN outside the covered span. The window is counted from the "
             "last complete bar, not from the market's close. A reading of 0 keeps the baseline (not the percentile's lowest "
             "rank); a reading of 1 ranks above the near-total mass of zeros, so its strength is 0.98 or more and the drift is "
             "close to the full 0.6σ times the clock. Only FOMC decisions are read here; CPI and payrolls dates live in the "
             "same table for the volatility models. sign = +1 (a positive reading leans the picture up), " + _CLOCK,
       trades="In the 24 hours before a Fed decision it puts more weight than the market on the ranges from about +0.5σ to "
              "+2σ above spot and buys those, thins every range below spot and buys none of them; strongest for closes "
              "6 to 12 hours out, a sixth of that an hour before the close. At all other times, and whenever the built-in "
              "calendar does not cover today, the picture is the baseline and the bot buys nothing."),
    _m("fourier_cycle", "Fourier Cycle", "Finds the strongest wave in the price and rides it a day ahead",
       "Cycle hunters believe prices move in waves: the same funding, settlement and session rhythms that create "
       "intraday seasonality can also leave a dominant periodic swing in the price. The model looks at the last 30 "
       "days of the log price, removes the straight-line trend, finds the strongest rhythm with a period between 12 "
       "hours and 10 days, fits that wave and projects it one day ahead. A wave heading up leans the picture bullish, "
       "one rolling over leans it bearish.",
       fourier_cycle, "Hann-windowed periodogram and least-squares sinusoid projection (lab implementation)",
       inputs="Hourly closes over the last 720 hours (30 days), re-read every 6 hours on the 00, 06, 12 and 18 UTC bars and "
              "held for the bars in between. Nothing is read until 720 bars are loaded, and the strength percentile needs 200 "
              "readings on top of that (about 920 bars). A gap in the 720-hour window makes that origin a missing value.",
       maths="At each origin the 720 log closes y are detrended by OLS on a constant and time; the residual is multiplied by a "
             "Hann window, zero-padded 4× (2,880 points) and its periodogram |FFT|² is pre-whitened by 4·sin²(πf), the "
             "inverse of a random walk's spectrum, so the peak is the frequency that stands out most against noise rather "
             "than the longest period allowed. The peak f is searched for periods 1/f between 12 and 240 hours. Then y is "
             "regressed jointly on 1, t, cos ωt, sin ωt with ω = 2πf, and the sinusoid's change over the next 24 hours, "
             "a·(cos ω(T+24) − cos ωT) + b·(sin ω(T+24) − sin ωT) with T the last index, is divided by the window's hourly "
             "return standard deviation times √24. The projection is 24 hours past the last bar, not to the market's close. "
             "Evaluations are memoised by origin so a new bar costs one fit. sign = +1 (a positive reading leans the picture "
             "up), " + _CLOCK,
       trades="When the fitted wave is due to rise over the next day it puts more weight than the market on the ranges from "
              "about +0.5σ to +2σ above spot and buys those, thinning the ranges below spot; when the wave is rolling over, "
              "the mirror image. Full strength for closes 6 to 12 hours away, half at two days. A projected change near the "
              "middle of its own history leaves the baseline and the bot buys nothing."),
    _m("ssa_trend", "SSA Trend", "Splits the price into trend, swings and noise; extends the trend",
       "Singular spectrum analysis splits the last 30 days of the log price into a smooth trend, slow oscillations "
       "and noise, without assuming any shape in advance: whatever structure repeats across overlapping week-long "
       "windows is kept, the rest is treated as noise. The model keeps the three strongest components, which carry "
       "the trend and the main swing, and extends them 24 hours ahead with the linear recurrence they satisfy. A "
       "rising forecast leans the picture bullish, a falling one bearish.",
       ssa_trend, "Singular spectrum analysis with recurrent forecasting (Golyandina, Nekrutkin and Zhigljavsky 2001, "
                  "Analysis of Time Series Structure: SSA and Related Techniques)",
       inputs="Hourly closes over the last 720 hours (30 days), re-read every 6 hours on the 00, 06, 12 and 18 UTC bars and "
              "held for the bars in between. Silent until 720 bars are loaded; the strength percentile needs 200 readings on "
              "top (about 920 bars). A gap in the window makes that origin a missing value.",
       maths="At each origin the 720 mean-removed log closes are embedded with window L = 168 into the 168 × 553 trajectory matrix "
             "X (column j = x_j .. x_j+167); the 3 leading eigenvectors U of X·Xᵀ are kept and the series is reconstructed by "
             "projecting onto them and diagonal-averaging (only the last 168 values are needed). With π the last row of U and "
             "ν² = |π|², the recurrent forecast uses coefficients R = (U_1..167 · π)/(1 − ν²) on the previous 167 reconstructed "
             "values and is iterated 24 steps. The signal is (forecast at +24 − last reconstructed value) / (s·√24), s the "
             "standard deviation of the window's hourly returns; the horizon is 24 hours past the last bar, not the market's "
             "close. A window with ν² = 1 (an undefined recurrence) is a missing value. Evaluations are memoised by origin so "
             "a new bar costs one decomposition. sign = +1 (a positive reading leans the picture up), " + _CLOCK,
       trades="When the extended components rise over the next day it puts more weight than the market on the ranges from "
              "about +0.5σ to +2σ above spot and buys those, thinning the ranges below spot and buying none; when they fall, "
              "the mirror image. Strongest for closes 6 to 12 hours out, fading towards the close and beyond a day. A forecast "
              "change near the middle of its own history leaves the baseline and the bot buys nothing."),
]
