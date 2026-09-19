"""Volatility pictures: the lab's family "Volatility" (`models/zoo_volatility.py`) and HAR-RV (`models/har_rv.py`).

These models do not lean up or down. Each defines a volatility signal; `vol_picture` widens or narrows the baseline
by exp(0.5 * sign * strength * 0.5), where strength is where the latest reading sits in its own history, within
[0.7, 1.6]. A signal without enough history leaves the baseline spread. HAR-RV forecasts the variance to the close
directly and applies the ratio to the baseline as a multiplier.

Not ported: `vrp_vol` and `implied_vol_scale` need Deribit's DVOL index.
"""
from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd

from .calendar import calendar_covers, covers_now, hours_to_next_event
from .core import VOL_MULT_BOUNDS, Ctx, Model, ModelUnavailable, cached, ewma_variance, hour_of_day_profile, view_picture, vol_picture
from .signals import hourly_vol, log_close, zscore

FAMILY = "Volatility"
FACTORS = ("volatility",)
BOOK = "Kakushadze and Serur, 151 Trading Strategies (2018)"
EWMA_SPAN = 32  # close to the baseline's lambda = 0.94
LN4 = 4.0 * np.log(2.0)
EVENT_KINDS = ("fomc", "cpi", "payrolls")


# ── signals ─────────────────────────────────────────────────

def squeeze_signal(bars: pd.DataFrame) -> pd.Series:
    width = log_close(bars).rolling(20, min_periods=20).std()
    return -zscore(np.log(width.where(width > 0)), 720)


def vol_level_signal(bars: pd.DataFrame) -> pd.Series:
    v = hourly_vol(bars, EWMA_SPAN)
    return np.log(v / v.rolling(2160, min_periods=720).median())


def leverage_signal(bars: pd.DataFrame) -> pd.Series:
    return -log_close(bars).diff(24) / (hourly_vol(bars) * np.sqrt(24))


def event_signal(bars: pd.DataFrame) -> pd.Series:
    """1 when a release is due within 24 hours of the bar close, 0 otherwise, NaN outside the calendar's span."""
    h = hours_to_next_event(pd.DatetimeIndex(bars.index), EVENT_KINDS)
    inside = ((h >= 0) & (h <= 24)).astype(float)
    return inside.where(calendar_covers(pd.DatetimeIndex(bars.index), EVENT_KINDS))


def weekday_hour_ratio(bars: pd.DataFrame, min_count: int = 8) -> pd.Series:
    """log of the expanding mean squared return in the next 24 hours' (weekday, hour) cells over the same hours'
    all-weekday means: the weekday part of the seasonality the hour-of-day profile does not already remove.
    The value at bar t uses returns up to bar t only."""
    r = log_close(bars).diff().to_numpy()
    idx = pd.DatetimeIndex(bars.index)
    key = (idx.dayofweek.to_numpy() * 24 + idx.hour.to_numpy()).astype(int)
    cell_sum = np.zeros(168)
    cell_n = np.zeros(168)
    out = np.full(r.size, np.nan)
    ahead = np.arange(1, 25)
    ready = False
    for t in range(r.size):
        if np.isfinite(r[t]):
            cell_sum[key[t]] += r[t] * r[t]
            cell_n[key[t]] += 1
        ready = ready or cell_n.min() >= min_count      # counts only grow
        if ready:
            cells = (key[t] + ahead) % 168
            num = (cell_sum[cells] / cell_n[cells]).mean()
            hour_mean = cell_sum.reshape(7, 24).sum(axis=0) / cell_n.reshape(7, 24).sum(axis=0)
            den = hour_mean[cells % 24].mean()
            if num > 0 and den > 0:
                out[t] = np.log(num / den)
    return pd.Series(out, index=bars.index)


def range_signal(bars: pd.DataFrame) -> pd.Series:
    hl = np.log(bars["high"].astype(float) / bars["low"].astype(float)).clip(lower=0)
    park = np.sqrt((hl**2).rolling(24, min_periods=24).mean() / LN4)
    cc = log_close(bars).diff().rolling(24, min_periods=24).std()
    return np.log(park.where(park > 0) / cc.where(cc > 0))


def volume_signal(bars: pd.DataFrame) -> pd.Series:
    v24 = bars["volume"].astype(float).clip(lower=0).rolling(24, min_periods=24).sum()
    med = v24.rolling(720, min_periods=240).median()
    return np.log((v24 + 1e-9) / (med + 1e-9))


def history_sign(bars: pd.DataFrame, signal: pd.Series, horizon: int = 24, min_obs: int = 200) -> int:
    """For a signal with no theoretical sign the lab follows the sign of the raw slope in its volatility table:
    log(realized variance over the next `horizon` hours / (horizon * EWMA variance)) regressed on the signal.
    Returns 0 when there is too little history to say."""
    logp = np.log(bars["close"].to_numpy(dtype=float))
    n = logp.size
    if n <= horizon + min_obs:
        return 0
    r = pd.Series(np.diff(logp), index=bars.index[1:])
    r_d = r.to_numpy() / hour_of_day_profile(r)[np.asarray(pd.DatetimeIndex(r.index).hour)]
    s2, s2_next = ewma_variance(r_d)
    sigma = np.maximum(np.concatenate([[np.sqrt(s2[0])], np.sqrt(s2), [np.sqrt(s2_next)]])[:n], 1e-6)
    csum = np.concatenate([[0.0], np.cumsum(np.diff(logp) ** 2)])
    t = np.arange(0, n - horizon)
    y = np.log((csum[t + horizon] - csum[t] + 1e-18) / (horizon * sigma[t] ** 2))
    x = signal.to_numpy(dtype=float)[t]
    ok = np.isfinite(x) & np.isfinite(y)
    if ok.sum() < min_obs:
        return 0
    xc, yc = x[ok] - x[ok].mean(), y[ok] - y[ok].mean()
    return 1 if float(np.dot(xc, yc)) >= 0 else -1


# ── models ──────────────────────────────────────────────────

def vol_squeeze(ctx: Ctx) -> np.ndarray:
    return vol_picture(ctx, cached(ctx, "vol_squeeze:signal", lambda: squeeze_signal(ctx.bars)), sign=1)


def vol_mean_reversion(ctx: Ctx) -> np.ndarray:
    return vol_picture(ctx, cached(ctx, "vol_mean_reversion:signal", lambda: vol_level_signal(ctx.bars)), sign=-1)


def leverage_effect_vol(ctx: Ctx) -> np.ndarray:
    return vol_picture(ctx, cached(ctx, "leverage_effect_vol:signal", lambda: leverage_signal(ctx.bars)), sign=1)


def event_vol(ctx: Ctx) -> np.ndarray:
    if not covers_now(ctx, EVENT_KINDS):
        return view_picture(ctx)
    sig = cached(ctx, "event_vol:signal", lambda: event_signal(ctx.bars))
    # outside the event window the picture keeps the baseline spread: a percentile rank would read "no event" as the
    # lowest reading ever and narrow it
    if not float(sig.iloc[-1]) > 0:
        return view_picture(ctx)
    return vol_picture(ctx, sig, sign=1)


def weekday_vol(ctx: Ctx) -> np.ndarray:
    return vol_picture(ctx, cached(ctx, "weekday_vol:signal", lambda: weekday_hour_ratio(ctx.bars)), sign=1)


def range_vol(ctx: Ctx) -> np.ndarray:
    sig = cached(ctx, "range_vol:signal", lambda: range_signal(ctx.bars))
    sign = cached(ctx, "range_vol:sign", lambda: history_sign(ctx.bars, sig))
    return vol_picture(ctx, sig, sign=sign) if sign else view_picture(ctx)


def volume_surge_vol(ctx: Ctx) -> np.ndarray:
    return vol_picture(ctx, cached(ctx, "volume_surge_vol:signal", lambda: volume_signal(ctx.bars)), sign=1)


# ── HAR-RV ──────────────────────────────────────────────────

def har_fit(bars: pd.DataFrame, horizons: tuple[int, ...] = (1, 2, 4, 8, 24, 48, 168), window_days: int = 120,
            min_hours: int = 336) -> dict[str, np.ndarray | float]:
    """Hourly realized variance from the Parkinson range estimator, the direct HAR regressions (log mean variance
    over the next h hours on log RV over the last 1, 24 and 168 hours), and the integrated variance each implies now."""
    bars = bars.iloc[-window_days * 24:]
    hi, lo = bars["high"].astype(float), bars["low"].astype(float)
    rv = (np.log(hi / lo) ** 2 / LN4).replace([np.inf, -np.inf], np.nan).dropna()
    rv = rv[rv > 0]
    if rv.size < min_hours:
        raise ModelUnavailable(f"har_rv needs {min_hours} hourly RV observations, has {rv.size}")
    # fill isolated gaps so the rolling windows are contiguous
    rv = rv.reindex(pd.date_range(rv.index[0], rv.index[-1], freq="h")).interpolate(limit=6).bfill().ffill()
    v = rv.to_numpy(dtype=float)
    n = v.size
    with np.errstate(invalid="ignore"):
        X = np.column_stack([np.ones(n), np.log(v), np.log(pd.Series(v).rolling(24).mean().to_numpy()),
                             np.log(pd.Series(v).rolling(168).mean().to_numpy())])
    cum = np.concatenate([[0.0], np.cumsum(v)])
    hs, iv = [], []
    for h in horizons:
        t_idx = np.arange(167, n - h)       # y_t = log mean(RV_{t+1..t+h})
        if t_idx.size < 50:
            continue
        y = np.log((cum[t_idx + 1 + h] - cum[t_idx + 1]) / h)
        beta, *_ = np.linalg.lstsq(X[t_idx], y, rcond=None)
        s2 = float(np.var(y - X[t_idx] @ beta))
        hs.append(float(h))
        iv.append(h * float(np.exp(X[-1] @ beta + 0.5 * s2)))      # mean hourly variance over the next h hours, times h
    if not hs or not np.all(np.isfinite(iv)):
        raise ModelUnavailable("har_rv: not enough history to fit any horizon")
    r = log_close(bars).diff().dropna()
    return {"hs": np.array(hs), "iv": np.array(iv), "rv_mean": float(np.mean(v[-24 * 30:])), "profile": hour_of_day_profile(r)}


def har_variance(fit: dict, now: float, end: float) -> float:
    """Forecast variance from `now` to `end`: integrated variance linear in h between the fitted horizons and
    unconditional beyond, per-step increments re-seasonalised by the hour-of-day profile."""
    bounds, hour_of, t = [], [], now
    while t < end - 1e-6:
        nxt = min((t // 3600 + 1) * 3600, end)
        hour_of.append(datetime.fromtimestamp(t, UTC).hour)
        bounds.append(nxt)
        t = nxt
    if not bounds:
        return 0.0
    hs, iv = fit["hs"], fit["iv"]
    tau = (np.array(bounds) - now) / 3600.0
    cum_iv = np.interp(tau, np.concatenate([[0.0], hs]), np.concatenate([[0.0], iv]))
    beyond = tau > hs[-1]
    cum_iv[beyond] = iv[-1] + (tau[beyond] - hs[-1]) * fit["rv_mean"]
    step_var = np.diff(np.concatenate([[0.0], np.maximum(cum_iv, 1e-12)]))
    return float(np.sum(step_var * fit["profile"][np.array(hour_of)] ** 2))


def har_rv(ctx: Ctx) -> np.ndarray:
    fit = cached(ctx, "har_rv:fit", lambda: har_fit(ctx.bars))
    var = har_variance(fit, ctx.now, ctx.end)
    if not np.isfinite(var) or var <= 0:
        raise ModelUnavailable("har_rv: no variance forecast")
    return view_picture(ctx, 0.0, vol_mult=float(np.clip(np.sqrt(var) / ctx.sigma, *VOL_MULT_BOUNDS)))


MODELS = [
    Model("vol_squeeze", "Bollinger Squeeze", FAMILY, "Tight Bollinger bands store up a big move",
          "The Bollinger squeeze: when the bands around the 20-hour average pinch unusually tight, a big move is often being "
          "stored up, because quiet stretches end and a market rarely stays coiled for long. The signal measures how compressed "
          "the band width is against the last month, the tighter the higher. A squeeze widens the picture of the coming day beyond "
          "what the recent quiet suggests; unusually wide bands narrow it.",
          vol_squeeze, factors=FACTORS, reference="Bollinger band squeeze (Bollinger 2001)",
          inputs="Hourly closes. Band width is the 20-hour standard deviation of the log close, its logarithm z-scored over a "
                 "720-hour (30-day) window that needs 180 bars; readings start at about bar 200 and the percentile needs 200 of "
                 "them, so about 400 bars in all.",
          maths="w_t = standard deviation of ln(close) over the last 20 bars (all 20 required); the signal is "
                "−(ln w_t − mean_720(ln w))/sd_720(ln w), the negative rolling z-score of the log band width over 720 hours with at "
                "least 180 bars, undefined where the rolling sd is not above 10⁻¹². sign = +1, vol_conviction 0.5 (the default), so "
                "the width multiplier is exp(0.25·s). A positive reading, bands tighter than the month's norm, widens the picture.",
          trades="When the bands are among the tightest of the loaded history the picture is up to 1.28 times the baseline's width, so it "
                 "puts more weight than the market on the ranges beyond roughly ±1.1σ on both sides and buys those, never the centre; when "
                 "the bands are unusually wide it narrows to 0.78 and buys the ranges within roughly ±0.9σ of spot. A width mid-history "
                 "leaves the baseline and the bot buys nothing.",
          pipeline="vol"),
    Model("vol_mean_reversion", "Volatility Reversion", FAMILY, "Storms blow over and calm spells end",
          "Volatility is mean-reverting: storms blow over and calm spells end, so the best guess for the next day's volatility "
          "sits between today's and the long-run level. The signal compares a short-term volatility with its 90-day median. When "
          "volatility is far above normal the picture narrows towards the long-run level faster than the baseline's fast EWMA "
          "assumes; when it is unusually quiet the picture widens.",
          vol_mean_reversion, factors=FACTORS, reference="Volatility mean reversion (Engle and Patton 2001, Quantitative Finance)",
          inputs="Hourly closes: the EWMA standard deviation of hourly log returns with span 32 (needing 48 bars), and its rolling "
                 "2,160-hour (90-day) median, which needs 720 bars (30 days). Readings start at about bar 770 and the percentile "
                 "needs 200 of them, so about 970 bars.",
          maths="v_t = exponentially weighted standard deviation of ℓ_t − ℓ_t−1 with span 32 (decay 31/33 ≈ 0.94, close to the "
                "baseline's λ), floored at 10⁻⁶; the signal is ln(v_t / median of v over the last 2,160 hours). sign = −1, "
                "vol_conviction 0.5: the width multiplier is exp(−0.25·s). A positive reading, volatility above its three-month "
                "median, narrows the picture.",
          trades="When short-term volatility is among the highest of the loaded history the picture is 0.78 of the baseline's width, "
                 "so it puts more weight than the market on the ranges within roughly ±0.9σ of spot and buys those, never the tails; "
                 "when volatility is among the lowest it widens to 1.28 and buys the ranges beyond roughly ±1.1σ on both sides. A "
                 "ratio near the middle of its history leaves the baseline and the bot buys nothing.",
          pipeline="vol"),
    Model("leverage_effect_vol", "Leverage Effect", FAMILY, "Falls create more volatility than rallies do",
          "The leverage effect: falls raise volatility more than rallies do, as forced sellers, liquidations and hedgers pile in "
          "after a drop while a rise draws calmer buying. The signal is minus the last day's return in units of volatility, so a "
          "sell-off reads high and a rally low. After a sell-off the picture of the next day widens; after a steady rally it "
          "narrows.",
          leverage_effect_vol, factors=FACTORS,
          reference="Leverage effect (Black 1976); asymmetric volatility (Glosten, Jagannathan, Runkle 1993)",
          inputs="Hourly closes over the last 24 hours and the EWMA hourly volatility from signals.hourly_vol (span 168 hours, 7 days, "
                 "needing 48 bars). Readings start at about bar 49 and the percentile needs 200 of them.",
          maths="The signal is −(ℓ_t − ℓ_t−24)/(v_t·√24): the negative 24-hour log return scaled to a one-day volatility, with v_t "
                "the span-168 EWM standard deviation of hourly log returns. sign = +1, vol_conviction 0.5, width multiplier "
                "exp(0.25·s). A positive reading, a fall over the last day, widens the picture.",
          trades="After the sharpest one-day falls in the loaded history the picture is up to 1.28 times the baseline's width, so it "
                 "puts more weight than the market on the ranges beyond roughly ±1.1σ on both sides and buys those, never the "
                 "centre; after the strongest rallies it narrows to 0.78 and buys the ranges within roughly ±0.9σ of spot. It never "
                 "leans up or down. A flat day leaves the baseline and the bot buys nothing.",
          pipeline="vol"),
    Model("volume_surge_vol", "Volume Surge", FAMILY, "Heavy volume today means bigger moves tomorrow",
          "Volume leads volatility: a surge in trading activity means news is being digested and positions moved, and the "
          "mixture-of-distributions view holds that returns are the sum of many information arrivals, so more arrivals mean bigger "
          "moves. The signal is the last 24 hours' volume against its median over the past month. Heavy volume widens the picture, "
          "thin volume narrows it.",
          volume_surge_vol, factors=FACTORS, reference="Mixture of distributions hypothesis (Clark 1973; Andersen 1996)",
          inputs="Hourly volume: the rolling 24-hour sum, and its rolling 720-hour (30-day) median, which needs 240 bars of sums (10 "
                 "days). Readings start at about bar 264 and the percentile needs 200 of them, so about 464 bars.",
          maths="V_t = Σ of the last 24 hours' volume (negative volumes clipped to 0); the signal is ln((V_t + 10⁻⁹)/(median of V "
                "over the last 720 hours + 10⁻⁹)). sign = +1, vol_conviction 0.5, width multiplier exp(0.25·s). A positive reading, "
                "a day of volume above the month's median, widens the picture.",
          trades="On the heaviest volume days of the loaded history the picture is up to 1.28 times the baseline's width: it puts "
                 "more weight than the market on the ranges beyond roughly ±1.1σ on both sides and buys those, thinning the centre; "
                 "on the thinnest days it narrows to 0.78 and buys the ranges within roughly ±0.9σ of spot. A day of ordinary volume "
                 "leaves the baseline and the bot buys nothing.",
          pipeline="vol"),
    Model("range_vol", "Candle Range", FAMILY, "Reads volatility from each hour's high and low, not just closes",
          "Candles carry more information than closes: Parkinson's estimator reads volatility from each hour's high and low, which "
          "a close-to-close measure misses when the price whipsaws inside the hour and ends where it began. The signal compares "
          "the last day's range-based volatility with its close-to-close volatility. The idea does not say whether a whipsawing "
          "day means a wider or a narrower day ahead, so the sign is taken from the loaded history: which way this reading has "
          "gone with the next day's realised variance.",
          range_vol, factors=FACTORS, reference="High-low range volatility estimator (Parkinson 1980)",
          inputs="Hourly high, low and close over the last 24 hours. Readings start at bar 24 and the percentile needs 200 of them; "
                 "the sign needs at least 200 bars where both the reading and the next 24 hours' realised variance exist (about "
                 "424 bars), otherwise the picture is the baseline.",
          maths="park_t = √(mean over the last 24 bars of ln(high/low)² / (4·ln 2)) and cc_t = standard deviation of the last 24 "
                "hourly log returns; the signal is ln(park_t/cc_t), undefined where either is zero. The sign is +1 if the covariance "
                "over the loaded bars between the signal and y_t = ln(Σ of the next 24 squared hourly log returns / (24·s²_t)) is "
                "non-negative and −1 otherwise, where s²_t is the EWMA (λ = 0.94) variance of hour-of-day-deseasonalised returns at "
                "t; with fewer than 200 usable bars the sign is 0 and the picture is the baseline. vol_conviction 0.5, width "
                "multiplier exp(0.25·sign·s). With sign +1 a positive reading, ranges wider than the closes suggest, widens the "
                "picture; with sign −1 it narrows it.",
          trades="Whichever way history has voted, at the extremes of its own history the picture is 1.28 or 0.78 times the "
                 "baseline's width: widened, it puts more weight than the market on the ranges beyond roughly ±1.1σ on both sides "
                 "and buys those; narrowed, it buys the ranges within roughly ±0.9σ of spot. It never leans up or down. A reading "
                 "mid-history, or a sign the history cannot settle, leaves the baseline and the bot buys nothing.",
          pipeline="vol"),
    Model("weekday_vol", "Weekday Volatility", FAMILY, "Wider into busy weekday sessions, narrower into weekends",
          "Bitcoin trades every day, but not every day is alike: weekends are usually quieter than weekdays, when stock and bond "
          "markets are open and US data is released, and Monday's Asian open differs from Friday's US close. The model learns, "
          "from history only, how volatile each hour of each weekday has been compared with the same hour on an average day, and "
          "widens the picture ahead of busy weekday sessions and narrows it into weekends. It adds the weekday part of the "
          "seasonality that the hour-of-day profile in the volatility clock does not already remove.",
          weekday_vol, factors=FACTORS, reference="Intraday and day-of-week volatility seasonality (Andersen and Bollerslev 1997)",
          inputs="Hourly closes, with each bar's UTC weekday and hour. Expanding sums of squared hourly log returns per (weekday, "
                 "hour) cell, 168 cells; a reading needs every cell seen at least 8 times (8 weeks, 1,344 bars) and the percentile "
                 "200 readings on top, so about 9 weeks of bars.",
          maths="For each of the 168 (weekday, hour) cells the model keeps the running sum and count of r² over the bars so far. At "
                "bar t, num = mean over the 24 cells following t's cell of the cell's mean squared return, den = mean over the same "
                "24 hours of the all-weekday mean squared return of that hour of day, and the signal is ln(num/den): the log ratio "
                "of the next day's weekday-specific variance to its hour-of-day-only variance. sign = +1, vol_conviction 0.5, width "
                "multiplier exp(0.25·s). A positive reading, the coming 24 hours busier than their hours of day usually are, widens "
                "the picture.",
          trades="Into the historically busiest weekday sessions the picture is up to 1.28 times the baseline's width, so it puts "
                 "more weight than the market on the ranges beyond roughly ±1.1σ on both sides and buys those; into a weekend it "
                 "narrows to 0.78 and buys the ranges within roughly ±0.9σ of spot. It never leans up or down. A day like the "
                 "average leaves the baseline and the bot buys nothing.",
          pipeline="vol"),
    Model("event_vol", "Macro Event Day", FAMILY, "Wider when a Fed decision, CPI or payrolls is due within a day",
          "Scheduled US macro releases move Bitcoin: the Federal Reserve's rate decision, the consumer price index and the "
          "payrolls report, whose surprises reprice rates and risk within minutes. When one is due within the next 24 hours the "
          "picture widens; otherwise it keeps the baseline spread rather than narrowing, since a quiet day is the norm and not "
          "evidence of unusual calm.",
          event_vol, factors=FACTORS,
          reference=f"Trading on economic announcements, {BOOK}, Section 19.5; Andersen, Bollerslev, Diebold, Vega (2003)",
          inputs="The bar timestamps and a release calendar built into the terminal: FOMC decisions (14:00 New York) from 2026-01-28 "
                 "to 2027-12-08 and CPI and payrolls (08:30 New York) for 2026. The model is live from 45 days before the first "
                 "listed release (payrolls, 2026-01-09) to the earliest last release of the three lists (payrolls, 2026-12-04); "
                 "outside that span, and outside an event window, the picture is the baseline. The percentile needs 200 bars of "
                 "readings.",
          maths="For each bar, h = hours from the bar's close (open + 1 hour) to the next listed release of any of the three kinds; "
                "the signal is 1 when 0 ≤ h ≤ 24 and 0 otherwise, undefined outside the calendar's span. It goes through the "
                "volatility pipeline only when the latest reading is 1, with sign = +1 and vol_conviction 0.5. Because 1 is the top "
                "of a history of 0s and 1s, the mid-rank percentile gives s = share of the loaded bars inside the span that were not "
                "within a day of a release, so the width multiplier is exp(0.25·(1 − share of event-window bars)): with three "
                "releases a month, "
                "about exp(0.225) ≈ 1.25. A latest reading of 0 goes straight to view_picture with shift 0 and multiplier 1 (a "
                "percentile would read no event as the lowest reading ever and narrow the picture). A reading of 1 widens the "
                "picture.",
          trades="In the 24 hours before a Fed decision, CPI or payrolls the picture is about 1.25 times the baseline's width, so it "
                 "puts more weight than the market on the ranges beyond roughly ±1.1σ on both sides and buys those, thinning the "
                 "centre and never buying it. It never leans up or down. On every other day, and outside the span the calendar "
                 "covers, the picture is the baseline and the bot buys nothing.",
          pipeline="vol"),
    Model("har_rv", "HAR-RV", "HAR", "Measures how wild today is, then bets on the range",
          "Volatility has memory at several speeds: today's, this week's and this month's realised variance each carry "
          "information about the next hours and days, which is Corsi's heterogeneous autoregression. Hourly realised variance is "
          "read from each candle's high and low, regressed at seven horizons on its own recent levels, and the fitted variance to "
          "the close, re-seasonalised hour by hour, sets how wide the picture is compared with the market's own EWMA clock. When "
          "the range-based forecast says the coming hours will be wilder than the clock's spread the picture widens; when calmer, "
          "it narrows.",
          har_rv, factors=FACTORS, reference="HAR-RV (Corsi 2009); high-low range estimator (Parkinson 1980)",
          inputs="Hourly high, low and close over the last 120 days (2,880 bars), needing at least 336 hourly ranges (14 days); gaps "
                 "of up to 6 hours are interpolated, the rest filled with the next valid value. The hour-of-day profile is refitted "
                 "on the same window's close-to-close returns. Without any fitted horizon, or a non-positive forecast, it draws no "
                 "picture and the runner skips it. Refitted once per bar.",
          maths="RV_t = ln(high_t/low_t)²/(4·ln 2), Parkinson's estimator. For each horizon h ∈ {1, 2, 4, 8, 24, 48, 168} hours an "
                "OLS regression of y_t = ln(mean of RV over t+1..t+h) on 1, ln RV_t, ln(mean RV over the last 24 hours) and ln(mean "
                "RV over the last 168 hours), over every origin with 168 hours behind it and h ahead (at least 50, otherwise the "
                "horizon is skipped); the forecast integrated variance is IV(h) = h·exp(x_now·β + s²/2), s² the residual variance "
                "(the log-normal correction). Cumulative variance to τ hours ahead is linear between the fitted horizons and "
                "extended beyond 168 hours at the mean hourly RV of the last 720 hours; the increments to each hourly step boundary "
                "are multiplied by the squared profile of the step's hour of day and summed to var. The picture is view_picture "
                "with mean_z = 0 and vol_mult = √var/σ_baseline, clipped to [0.7, 1.6]: a range-based forecast set against the "
                "close-based EWMA clock, not a percentile of its own history. Terminal version: the lab uses 5-minute realised "
                "variance when it has it and simulates paths; here the ratio is a width multiplier. A forecast above the clock "
                "widens the picture.",
          trades="When the HAR forecast exceeds the baseline σ the picture is up to 1.6 times as wide, so it puts more weight than the "
                 "market on the ranges beyond roughly ±1.2σ on both sides and buys those, never the centre; when the forecast is lower it "
                 "is as narrow as 0.7 and buys the ranges within roughly ±0.85σ of spot. It never leans up or down. It is the baseline only "
                 "when the two forecasts agree within a few percent, and then buys nothing.",
          pipeline="vol"),
]
