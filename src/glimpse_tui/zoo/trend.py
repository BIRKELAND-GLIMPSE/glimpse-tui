"""Trend-following pictures: the lab's `directional.py` trend models and `zoo_trend.py`, family "Trend".

Each model defines one causal signal from the hourly bars, and a positive reading means the trend points up
(`sign=1`). `conviction_picture` turns the latest reading into a picture: the drift follows where the reading sits in
the signal's own history, and the tail on the trend's side is fattened.
"""
from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pandas as pd

from .core import Ctx, Model, cached, conviction_picture
from .signals import atr, daily_levels, ema, heikin_ashi, hourly_vol, log_close, rolling_slope_t, signed_run_length, sma, zscore

BOOK = "Kakushadze and Serur, 151 Trading Strategies (2018)"
FACTORS = ("direction", "tails")


def _trend(model_id: str, signal: Callable[[pd.DataFrame], pd.Series]) -> Callable[[Ctx], np.ndarray]:
    """A model function: the signal series is built once per set of bars, the picture once per close."""
    def fn(ctx: Ctx) -> np.ndarray:
        return conviction_picture(ctx, cached(ctx, f"{model_id}:signal", lambda: signal(ctx.bars)), sign=1, family="Trend")

    fn.__name__ = model_id
    return fn


# ── the first five (lab: directional.py) ────────────────────

def tsmom_signal(bars: pd.DataFrame, lookbacks: tuple[int, ...] = (24, 72, 168, 720)) -> pd.Series:
    lp, vol = log_close(bars), hourly_vol(bars)
    parts = [(lp - lp.shift(n)) / (vol * np.sqrt(n)) for n in lookbacks]
    return pd.concat(parts, axis=1).mean(axis=1)


def ema_crossover_signal(bars: pd.DataFrame, fast: int = 20, slow: int = 50) -> pd.Series:
    lp = log_close(bars)
    return (ema(lp, fast) - ema(lp, slow)) / (hourly_vol(bars) * np.sqrt(slow))


def macd_signal(bars: pd.DataFrame) -> pd.Series:
    lp = log_close(bars)
    macd = ema(lp, 12) - ema(lp, 26)
    hist = macd - macd.ewm(span=9, min_periods=9).mean()
    return hist / (hourly_vol(bars) * np.sqrt(26))


def donchian_signal(bars: pd.DataFrame, channel_hours: int = 48) -> pd.Series:
    n = channel_hours
    hi = bars["high"].astype(float).rolling(n, min_periods=n).max()
    lo = bars["low"].astype(float).rolling(n, min_periods=n).min()
    mid = (hi + lo) / 2
    half = ((hi - lo) / 2).clip(lower=1e-9)
    pos = ((bars["close"].astype(float) - mid) / half).clip(-1, 1)
    return pos**3                                 # the cube keeps the middle of the channel quiet


def local_linear_trend(y: np.ndarray, obs_noise: float, slope_noise: float, level_noise: float = 1.0) -> tuple[np.ndarray, np.ndarray]:
    """Filtered level and slope of the local linear trend model, forward only. Only the ratios of the noise
    variances matter: the filter is linear, so the scale of `y` drops out."""
    n = y.size
    level, slope_out = np.full(n, np.nan), np.full(n, np.nan)
    ok = np.isfinite(y)
    if ok.sum() < 3:
        return level, slope_out
    first = int(np.argmax(ok))
    lvl, slope = float(y[first]), 0.0
    p00, p01, p11 = 1e6, 0.0, 1e6
    for t in range(first, n):
        # predict: x = F x, P = F P F' + Q with F = [[1, 1], [0, 1]], Q = diag(level_noise, slope_noise)
        lvl = lvl + slope
        p00, p01, p11 = p00 + 2 * p01 + p11 + level_noise, p01 + p11, p11 + slope_noise
        yt = y[t]
        if yt == yt:  # finite
            s = p00 + obs_noise
            k0, k1 = p00 / s, p01 / s
            e = yt - lvl
            lvl, slope = lvl + k0 * e, slope + k1 * e
            p00, p01, p11 = p00 - k0 * p00, p01 - k0 * p01, p11 - k1 * p01
        level[t], slope_out[t] = lvl, slope
    return level, slope_out


def kalman_signal(bars: pd.DataFrame, obs_noise: float = 0.1, slope_noise: float = 4e-4, warmup: int = 72) -> pd.Series:
    """The lab estimates the three noise variances by maximum likelihood (statsmodels). Here they are fixed: the level
    moves like the price itself, and a slope noise of 1/2500 of that makes the filtered slope settle at a gain of
    about 0.02 per hour, a memory of roughly four days."""
    lp = log_close(bars).iloc[-2000:]
    _, slope = local_linear_trend(lp.to_numpy(), obs_noise, slope_noise)
    slope[:warmup] = np.nan                       # the diffuse start is the first few returns, not a trend
    out = pd.Series(np.nan, index=bars.index)
    out.loc[lp.index] = slope / hourly_vol(bars).reindex(lp.index).to_numpy()
    return out


# ── the 151 Trading Strategies wave (lab: zoo_trend.py) ─────

def sma_price_filter_signal(bars: pd.DataFrame, window: int = 168) -> pd.Series:
    lp = log_close(bars)
    return (lp - sma(lp, window)) / (hourly_vol(bars) * np.sqrt(window))


def sma_triple_filter_signal(bars: pd.DataFrame) -> pd.Series:
    lp, vol = log_close(bars), hourly_vol(bars)
    m1, m2, m3 = sma(lp, 24), sma(lp, 72), sma(lp, 168)
    g1 = (m1 - m2) / (vol * np.sqrt(72))
    g2 = (m2 - m3) / (vol * np.sqrt(168))
    up = (g1 > 0) & (g2 > 0)
    dn = (g1 < 0) & (g2 < 0)
    out = pd.Series(0.0, index=bars.index)
    out[up] = np.minimum(g1[up], g2[up])
    out[dn] = np.maximum(g1[dn], g2[dn])
    return out.where(g1.notna() & g2.notna())


def one_sided_hp(y: np.ndarray, lam: float) -> np.ndarray:
    """One-sided Hodrick-Prescott trend: the Kalman filter of the local linear trend model whose smoother is the HP
    filter (observation noise 1, no level noise, slope noise 1 / lam), run forward only, so the trend at t uses y up
    to t."""
    return local_linear_trend(y, obs_noise=1.0, slope_noise=1.0 / lam, level_noise=0.0)[0]


def hp_filter_signal(bars: pd.DataFrame, hp_lambda: float = 1.0e6) -> pd.Series:
    trend = pd.Series(one_sided_hp(log_close(bars).to_numpy(), hp_lambda), index=bars.index)
    return (ema(trend, 24) - ema(trend, 72)) / (hourly_vol(bars) * np.sqrt(72))


def trend_r_squared_signal(bars: pd.DataFrame) -> pd.Series:
    slope, r2 = rolling_slope_t(log_close(bars), 168)
    return slope * 168 / (hourly_vol(bars) * np.sqrt(168)) * r2


def momentum_skip_day_signal(bars: pd.DataFrame) -> pd.Series:
    lp = log_close(bars)
    return (lp.shift(24) - lp.shift(720)) / (hourly_vol(bars) * np.sqrt(696))


def pivot_breakout_signal(bars: pd.DataFrame) -> pd.Series:
    lv = daily_levels(bars)
    rng = (lv["R1"] - lv["S1"]).where(lambda x: x > 0)
    return ((bars["close"].astype(float) - lv["P"]) / rng).clip(-3, 3)


def adx_directional_signal(bars: pd.DataFrame) -> pd.Series:
    h, lo = bars["high"].astype(float), bars["low"].astype(float)
    up, dn = h.diff(), -lo.diff()
    plus_dm = up.where((up > dn) & (up > 0), 0.0)
    minus_dm = dn.where((dn > up) & (dn > 0), 0.0)
    a = atr(bars, 14)
    plus_di = 100 * plus_dm.ewm(alpha=1 / 14, min_periods=14).mean() / a
    minus_di = 100 * minus_dm.ewm(alpha=1 / 14, min_periods=14).mean() / a
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).where(lambda x: x > 0)
    adx = dx.ewm(alpha=1 / 14, min_periods=14).mean()
    return (plus_di - minus_di) / 100 * (adx / 25)


def keltner_breakout_signal(bars: pd.DataFrame) -> pd.Series:
    c = bars["close"].astype(float)
    return ((c - ema(c, 20)) / (2 * atr(bars, 20))).clip(-3, 3)


def supertrend_signal(bars: pd.DataFrame, n: int = 10, mult: float = 3.0) -> pd.Series:
    h, lo, c = (bars[k].astype(float).to_numpy() for k in ("high", "low", "close"))
    a = atr(bars, n).to_numpy()
    mid = (h + lo) / 2
    upper, lower = mid + mult * a, mid - mult * a
    out = np.full(c.size, np.nan)
    fu, fl, direction = np.nan, np.nan, 1
    for i in range(c.size):
        if not np.isfinite(a[i]):
            continue
        fu = upper[i] if not np.isfinite(fu) or upper[i] < fu or c[i - 1] > fu else fu
        fl = lower[i] if not np.isfinite(fl) or lower[i] > fl or c[i - 1] < fl else fl
        if direction == 1 and c[i] < fl:
            direction = -1
        elif direction == -1 and c[i] > fu:
            direction = 1
        band = fl if direction == 1 else fu
        out[i] = (c[i] - band) / a[i]
    return pd.Series(out, index=bars.index).clip(-6, 6)


def parabolic_sar(bars: pd.DataFrame, step: float = 0.02, max_af: float = 0.2) -> pd.Series:
    h, lo = bars["high"].astype(float).to_numpy(), bars["low"].astype(float).to_numpy()
    n = h.size
    sar = np.full(n, np.nan)
    if n < 3:
        return pd.Series(sar, index=bars.index)
    up = True
    af, ep, s = step, h[0], lo[0]
    for i in range(1, n):
        s = s + af * (ep - s)
        if up:
            s = min(s, lo[i - 1], lo[i - 2] if i > 1 else lo[i - 1])
            if lo[i] < s:
                up, s, ep, af = False, ep, lo[i], step
            elif h[i] > ep:
                ep, af = h[i], min(af + step, max_af)
        else:
            s = max(s, h[i - 1], h[i - 2] if i > 1 else h[i - 1])
            if h[i] > s:
                up, s, ep, af = True, ep, h[i], step
            elif lo[i] < ep:
                ep, af = lo[i], min(af + step, max_af)
        sar[i] = s
    return pd.Series(sar, index=bars.index)


def parabolic_sar_signal(bars: pd.DataFrame) -> pd.Series:
    return ((bars["close"].astype(float) - parabolic_sar(bars)) / atr(bars, 14)).clip(-8, 8)


def ichimoku_signal(bars: pd.DataFrame) -> pd.Series:
    h, lo, c = (bars[k].astype(float) for k in ("high", "low", "close"))

    def mid(n: int) -> pd.Series:
        return (h.rolling(n, min_periods=n).max() + lo.rolling(n, min_periods=n).min()) / 2

    tenkan, kijun = mid(9), mid(26)
    span_a = ((tenkan + kijun) / 2).shift(26)
    span_b = mid(52).shift(26)
    top, bottom = np.maximum(span_a, span_b), np.minimum(span_a, span_b)
    a = atr(bars, 26)
    above = ((c - top) / a).where((c > top) & (tenkan > kijun), 0.0)
    below = ((c - bottom) / a).where((c < bottom) & (tenkan < kijun), 0.0)
    return (above + below).where(span_b.notna()).clip(-6, 6)


def _bars_since(x: np.ndarray, n: int, extreme: Callable[..., np.ndarray]) -> np.ndarray:
    """Bars since the extreme of the last n + 1 bars (the first one on a tie, as the lab's rolling apply does)."""
    out = np.full(x.size, np.nan)
    if x.size > n:
        w = np.lib.stride_tricks.sliding_window_view(x, n + 1)
        since = (n - extreme(w, axis=1)).astype(float)
        since[~np.isfinite(w).all(axis=1)] = np.nan
        out[n:] = since
    return out


def aroon_signal(bars: pd.DataFrame, n: int = 25) -> pd.Series:
    since_high = _bars_since(bars["high"].to_numpy(dtype=float), n, np.argmax)
    since_low = _bars_since(bars["low"].to_numpy(dtype=float), n, np.argmin)
    return pd.Series(((n - since_high) - (n - since_low)) / n, index=bars.index)


def heikin_ashi_signal(bars: pd.DataFrame) -> pd.Series:
    ha = heikin_ashi(bars)
    run = signed_run_length(np.sign((ha["close"] - ha["open"]).to_numpy()))
    return pd.Series(np.tanh(run / 6.0), index=bars.index)


def obv_signal(bars: pd.DataFrame) -> pd.Series:
    c, v = bars["close"].astype(float), bars["volume"].astype(float).clip(lower=0)
    obv = (np.sign(c.diff()).fillna(0.0) * v).cumsum()
    return zscore(obv.diff(72), 720)


def variance_ratio_signal(bars: pd.DataFrame) -> pd.Series:
    lp = log_close(bars)
    r1 = lp.diff()
    r24 = lp.diff(24)
    var1 = (r1**2).rolling(336, min_periods=168).mean()
    var24 = (r24**2).rolling(336, min_periods=168).mean()
    vr = var24 / (24 * var1.where(var1 > 0))
    mom = r24 / (hourly_vol(bars) * np.sqrt(24))
    return (vr - 1.0).clip(-1, 2) * np.tanh(mom)


def _m(model_id: str, name: str, blurb: str, description: str, signal: Callable[[pd.DataFrame], pd.Series], reference: str, *,
       inputs: str = "", maths: str = "", trades: str = "", pipeline: str = "conviction") -> Model:
    return Model(model_id, name, "Trend", blurb, description, _trend(model_id, signal), factors=FACTORS, reference=reference,
                 inputs=inputs, maths=maths, trades=trades, pipeline=pipeline)


# Every model below goes through conviction_picture with sign=+1, family Trend and no overrides: conviction 0.6,
# shape_conviction 0.45, timing "ride", shape "trend". v_t in the maths is signals.hourly_vol: the exponentially
# weighted standard deviation of hourly log returns with span 168 hours, needing 48 returns, floored at 10⁻⁶.

MODELS = [
    _m("tsmom_drift", "Momentum", "Buys what has already been going up",
       "Time-series momentum: an asset that has been rising tends to keep rising for a while, and one that has been "
       "falling tends to keep falling. Bitcoin is checked against its own price 1, 3, 7 and 30 days ago, each gap "
       "measured in units of its own volatility, and the four readings are averaged so no single horizon dominates. "
       "Bullish after rallies, bearish after sell-offs, quiet when the horizons disagree.",
       tsmom_signal, "Time-series momentum (Moskowitz, Ooi, Pedersen 2012)",
       inputs="Hourly closes over the last 720 hours (30 days) and the EWMA hourly volatility from signals.hourly_vol. "
              "A reading exists once 720 bars are loaded; the strength percentile needs 200 readings on top of that, "
              "so the picture leaves the baseline from about 920 bars.",
       maths="With ℓ = ln(close) and v_t the hourly volatility (EWM standard deviation of hourly log returns, span 168 "
             "hours), the signal at bar t is the mean over n ∈ {24, 72, 168, 720} of (ℓ_t − ℓ_t−n) / (v_t·√n): four "
             "volatility-scaled log returns, one per lookback, averaged with equal weight. sign = +1 (a positive reading "
             "leans the picture up), family Trend: the drift rides at full strength at every horizon and the tail on the "
             "signal's side is fattened. Terminal version: the lab regresses future returns on this signal; here the lean "
             "comes from the reading's percentile in its own history.",
       trades="After a run of rallies it puts more weight than the market on the ranges from spot to roughly two σ "
              "above it and on the far upside, and buys those; it thins the ranges below spot and does not buy them. "
              "After sell-offs the mirror image. When the four horizons cancel, the reading sits mid-history, the picture "
              "is the baseline and the bot buys nothing."),
    _m("ema_crossover", "EMA Crossover", "Fast average above the slow one is bullish, below is bearish",
       "The moving-average crossover every charting tool ships with: a fast exponential average of the price is "
       "compared with a slow one. The fast average tracks the last day or so, the slow one the last few days, so when "
       "the fast sits above the slow the recent price is above its own recent past, which is what a trend looks like. "
       "The gap is measured in units of volatility, so a wide gap in a quiet market counts for more than the same gap "
       "in a wild one.",
       ema_crossover_signal, "Moving-average trading rules (Brock, Lakonishok, LeBaron 1992)",
       inputs="Hourly closes only, through two exponential averages of 20 and 50 hours, plus the 168-hour EWMA hourly "
              "volatility. The first reading arrives at bar 50; with 200 readings after that the picture can leave the "
              "baseline, well inside the 400-bar minimum.",
       maths="ℓ = ln(close). EMA(ℓ, n) is the exponentially weighted mean with span n, weight α = 2/(n + 1) on the newest "
             "bar (α ≈ 0.095 for 20 hours, ≈ 0.039 for 50 hours), undefined until n bars are in. The signal at bar t is "
             "(EMA(ℓ, 20)_t − EMA(ℓ, 50)_t) / (v_t·√50): the fast-minus-slow gap in log price, divided by the volatility "
             "expected over the slow window's length. No clip, no further transform. sign = +1 with the Trend defaults: "
             "a positive reading (fast above slow) leans the picture up and fattens the upper tail.",
       trades="Fast above slow by a lot, relative to this signal's own past, and it puts more weight than the market on "
              "the ranges just above spot out to about +2σ and on the far upside, buying those; ranges below spot are "
              "thinned and not bought. Fast below slow flips the picture. A gap near its historical median leaves the "
              "baseline and buys nothing."),
    _m("macd_drift", "MACD", "Follows the MACD histogram on the hourly chart",
       "MACD (12, 26, 9) on the hourly chart: the MACD line is the gap between a 12-hour and a 26-hour exponential "
       "average, and its signal line is a 9-hour average of that gap. The histogram is the MACD line minus its signal "
       "line, so it is positive when the trend gap is widening faster than its own smoothed version, the classic sign "
       "that momentum is building. A positive histogram leans bullish, a negative one bearish.",
       macd_signal, "Moving average convergence divergence (Appel 1979); Brock, Lakonishok, LeBaron (1992)",
       inputs="Hourly closes only, plus the 168-hour EWMA hourly volatility. The 26-hour average needs 26 bars and the "
              "signal line 9 finite MACD values after that, so the first reading arrives at about bar 34; the 200 "
              "readings the percentile needs come well before the 400-bar minimum.",
       maths="ℓ = ln(close). MACD_t = EMA(ℓ, 12)_t − EMA(ℓ, 26)_t with EMA(·, n) the span-n exponentially weighted mean "
             "(α = 2/(n + 1), needing n bars). The signal line is the span-9 EWM mean of MACD (α = 0.2, needing 9 MACD "
             "values). The histogram H_t = MACD_t − signal_t is divided by v_t·√26, the volatility expected over the slow "
             "span, to give the reading. No clip. sign = +1 with the Trend defaults: a positive histogram leans the "
             "picture up and fattens the upper tail. It is the histogram's level that matters, not its change.",
       trades="A histogram high in its own history puts more weight than the market on the ranges from spot to roughly "
              "+2σ and on the far upside and buys them, while thinning the ranges below spot, which it never buys. A "
              "deeply negative histogram does the reverse. A histogram near zero, which is most of the time, sits "
              "mid-history: baseline picture, no trades."),
    _m("donchian_breakout", "Donchian Breakout", "Leans with a breakout from the 48-hour high-low channel",
       "Donchian channel breakout, the rule behind the Turtle traders: a close at the top of its 48-hour high-low "
       "channel is a breakout, a close at the bottom a breakdown, and a close in the middle is noise. The position "
       "inside the channel is cubed so the middle of the channel counts for almost nothing and only the edges tilt the "
       "picture. A new two-day high leans bullish, a new two-day low bearish.",
       donchian_signal, "Donchian channel breakout (Donchian 1960); Brock, Lakonishok, LeBaron (1992) trading-range break",
       inputs="Hourly high, low and close, in price (not log) units, over a rolling 48-hour (2-day) window that includes "
              "the current bar. No volatility scaling. The first reading arrives at bar 48; the percentile needs 200 "
              "readings, inside the 400-bar minimum.",
       maths="hi_t = max(high) and lo_t = min(low) over the last 48 bars including bar t; mid = (hi + lo)/2; "
             "half = max((hi − lo)/2, 10⁻⁹). Position p_t = clip((close_t − mid)/half, −1, 1), which is +1 exactly when "
             "the close is the 48-hour high and −1 when it is the low (the close can never leave the channel because the "
             "current bar is in it, so the clip only guards the degenerate case). The reading is p_t³: 0.5 becomes "
             "0.125, 0.9 becomes 0.73. sign = +1 with the Trend defaults; a positive reading leans the picture up.",
       trades="At or near a fresh 48-hour high it puts more weight than the market on the ranges from spot to about "
              "+2σ and on the far upside and buys them; the ranges below spot are thinned and left alone. A fresh "
              "48-hour low mirrors that. Inside the channel the cubed reading is near zero and near mid-history, so the "
              "picture is the baseline and nothing is bought."),
    _m("kalman_trend", "Kalman Trend", "Tracks the hidden slope under the noise",
       "The log price is treated as a hidden straight line whose level and slope both wander a little each hour, seen "
       "through observation noise. A Kalman filter tracks that hidden slope forward through the noise, updating it a "
       "little with every surprise in the price. The current filtered slope, in units of volatility, is the signal: a "
       "positive slope leans bullish, a negative one bearish, and the filter reacts slowly so a single wild hour does "
       "not flip it.",
       kalman_signal, "Local linear trend model (Harvey 1989)",
       inputs="Hourly closes over the last 2,000 hours (about 83 days) only, plus the 168-hour EWMA hourly volatility. "
              "The filter restarts at the oldest of those bars and its first 72 slopes are discarded, so readings exist "
              "for at most 1,928 bars and the percentile is taken over those (200 are needed).",
       maths="Local linear trend Kalman filter on y = ln(close): state (level, slope), level ← level + slope, slope ← "
             "slope, process noise Q = diag(1.0, 4·10⁻⁴), observation noise R = 0.1 (only the ratios matter: the slope "
             "noise is 1/2500 of the level noise). Start at the first finite y, slope 0, covariance diag(10⁶, 10⁶); each "
             "hour predict P ← F P Fᵀ + Q, then with e = y_t − level and S = P₀₀ + R update level += e·P₀₀/S, "
             "slope += e·P₀₁/S; missing bars skip the update. The reading is slope_t / v_t (log-price per hour over "
             "volatility per hour), with the first 72 slopes set to NaN as warm-up. The lab fits the three variances by "
             "maximum likelihood (statsmodels); here they are fixed so the slope gain settles near 0.02 per hour, a "
             "memory of about four days. sign = +1 with the Trend defaults: a positive slope leans the picture up.",
       trades="A filtered slope high in its own history puts more weight than the market on the ranges from spot to "
              "roughly +2σ and on the far upside and buys them, thinning the ranges below spot, which it never buys. A "
              "strongly negative slope does the reverse. Because the filter is slow, the reading changes gradually and "
              "the picture drifts back to the baseline over days, not hours; near mid-history it buys nothing."),
    _m("sma_price_filter", "Moving Average Filter", "Bullish above the 7-day average, bearish below it",
       "The simplest trend rule there is: is Bitcoin above or below its 7-day simple moving average? The belief is that "
       "a price above its own recent average is being bid up and will tend to stay above it for a while. The gap "
       "between the log price and the average is measured in units of volatility, so the same distance means more in "
       "a calm market. Above the average leans bullish, below it bearish.",
       sma_price_filter_signal, f"Single moving average, {BOOK}, Section 3.11",
       inputs="Hourly closes only, through a 168-hour (7-day) simple moving average, plus the 168-hour EWMA hourly "
              "volatility. The first reading arrives at bar 168; the 200 readings the percentile needs arrive by bar 368, "
              "inside the 400-bar minimum.",
       maths="ℓ = ln(close) and SMA(ℓ, 168)_t the plain mean of the last 168 values (undefined until 168 bars are in). "
             "The reading at bar t is (ℓ_t − SMA(ℓ, 168)_t) / (v_t·√168): the log gap to the average in units of the "
             "volatility expected over one window length. No clip, no transform. sign = +1 with the Trend defaults: a "
             "positive reading (price above its average) leans the picture up and fattens the upper tail.",
       trades="Far above the 7-day average, by this signal's own standards, it puts more weight than the market on the "
              "ranges from spot to about +2σ and on the far upside and buys them; the ranges below spot are thinned and "
              "not bought. Far below the average is the mirror image. Sitting on the average, the reading is "
              "mid-history, the picture is the baseline and nothing is bought."),
    _m("sma_triple_filter", "Triple Average Filter", "Leans only when the 1, 3 and 7-day averages line up",
       "Three moving averages of 1, 3 and 7 days must stack in order before this model believes in a trend: fast "
       "above middle above slow is an uptrend, the reverse a downtrend, anything tangled is no trend at all. The "
       "reading is the weaker of the two gaps, so both must be wide before the lean is strong. A picture for traders "
       "who want confirmation before they lean, at the cost of being late.",
       sma_triple_filter_signal, f"Three moving averages, {BOOK}, Section 3.13",
       inputs="Hourly closes only, through simple moving averages of 24, 72 and 168 hours, plus the 168-hour EWMA hourly "
              "volatility. The first reading arrives at bar 168 (an exact 0 counts as a reading); the percentile needs "
              "200 of them, inside the 400-bar minimum.",
       maths="ℓ = ln(close); m₁, m₂, m₃ = SMA(ℓ, 24), SMA(ℓ, 72), SMA(ℓ, 168). Gaps g₁ = (m₁ − m₂)/(v_t·√72) and "
             "g₂ = (m₂ − m₃)/(v_t·√168). If g₁ > 0 and g₂ > 0 the reading is min(g₁, g₂); if g₁ < 0 and g₂ < 0 it is "
             "max(g₁, g₂) (the one nearer zero); otherwise it is exactly 0. NaN until both gaps exist. sign = +1 with the "
             "Trend defaults. A reading of 0 is ranked at the middle of the block of past zeros, so a tangled market "
             "reads as near-neutral rather than as a low.",
       trades="With all three averages stacked upward and both gaps wide by this signal's own history, it puts more "
              "weight than the market on the ranges from spot to about +2σ and on the far upside and buys them; ranges "
              "below spot are thinned and never bought. Stacked downward, the mirror image. Whenever the averages are "
              "tangled the reading is 0, the picture stays close to the baseline and the bot buys nothing."),
    _m("hp_filter_trend", "HP Filter Trend", "A crossover on the smoothed trend line instead of the raw price",
       "The Hodrick-Prescott filter separates a smooth trend from noise, the way macroeconomists split GDP into trend "
       "and cycle. This model runs a one-sided version forward in time so nothing from the future leaks in, then "
       "applies a 1-day against 3-day exponential-average crossover to the smooth trend line instead of the raw price, "
       "which removes most whipsaws. A rising trend line leans bullish, a falling one bearish.",
       hp_filter_signal, f"Moving averages with HP filter, {BOOK}, Section 8.1; Hodrick and Prescott (1997)",
       inputs="Hourly closes only, over the whole loaded history, plus the 168-hour EWMA hourly volatility. The filter "
              "starts at the first bar with no warm-up discarded; the first reading arrives at bar 72 when the slow "
              "average is full, and the 200 readings the percentile needs are inside the 400-bar minimum.",
       maths="The one-sided HP trend τ_t is the filtered level of a local linear trend Kalman filter on y = ln(close) "
             "with observation noise 1, level noise 0 and slope noise 1/λ, λ = 10⁶ (its two-sided smoother is the HP "
             "filter; the half-power period of that smoother is about 200 hours), started at the first finite y with "
             "slope 0 and covariance diag(10⁶, 10⁶), run forward only. The reading at bar t is "
             "(EMA(τ, 24)_t − EMA(τ, 72)_t) / (v_t·√72), EMA(·, n) the span-n exponentially weighted mean needing n bars. "
             "No clip. sign = +1 with the Trend defaults: a positive reading (the trend line's fast average above its "
             "slow one) leans the picture up and fattens the upper tail.",
       trades="When the smoothed trend is bending up hard, relative to its own past, it puts more weight than the market "
              "on the ranges from spot to about +2σ and on the far upside and buys them; the ranges below spot are "
              "thinned and not bought. Bending down, the reverse. Because the trend line is very smooth, the reading "
              "changes slowly; near its median the picture is the baseline and nothing is bought."),
    _m("trend_r_squared", "Clean Trend (R-squared)", "Trusts steady staircases, ignores jagged zigzags",
       "Not every trend is equal: a steady staircase is more convincing than a jagged zigzag with the same net move. "
       "This model fits a straight line through the last 7 days of log prices and multiplies the fitted rise, in "
       "volatility units, by how well the line fits (its R²), so only clean, orderly trends produce a strong reading "
       "and a noisy chart with the same slope is discounted. Clean uptrends lean bullish, clean downtrends bearish.",
       trend_r_squared_signal, f"R-squared, {BOOK}, Section 4.3",
       inputs="Hourly closes only, over a rolling 168-hour (7-day) window, plus the 168-hour EWMA hourly volatility. The "
              "first reading arrives at bar 168 (a perfectly flat window gives NaN); the 200 readings the percentile "
              "needs are inside the 400-bar minimum.",
       maths="Rolling ordinary least squares of ℓ = ln(close) on the bar index over the last 168 bars, by rolling sums: "
             "slope b_t = cov(t, ℓ)/var(t) and R²_t = cov²/(var(t)·var(ℓ)), clipped to [0, 1] and NaN when var(ℓ) ≤ "
             "10⁻¹⁸. The reading is b_t·168 / (v_t·√168) × R²_t: the fitted rise over the window, in units of the "
             "volatility expected over the window, times the fraction of variance the line explains. No clip. sign = +1 "
             "with the Trend defaults: a positive reading leans the picture up and fattens the upper tail.",
       trades="A clean, steep week up puts more weight than the market on the ranges from spot to about +2σ and on the "
              "far upside and buys them, thinning the ranges below spot, which it never buys; a clean week down is the "
              "mirror image. A zigzag with a low R² reads near zero whatever its slope, sits mid-history, leaves the "
              "baseline and buys nothing."),
    _m("momentum_skip_day", "Monthly Momentum (skip a day)", "The 30-day move, leaving out the most recent day",
       "Classic price momentum with a twist borrowed from equity research: measure the 30-day move but skip the most "
       "recent day, because very short-term moves tend to reverse and blur the signal. What matters is where the price "
       "stood yesterday relative to a month ago, in units of volatility. A strong month leans the picture bullish, a "
       "weak one bearish, and today's wobble is ignored.",
       momentum_skip_day_signal, f"Price-momentum, {BOOK}, Section 3.1; Jegadeesh and Titman (1993)",
       inputs="Hourly closes over the last 720 hours (30 days), plus the 168-hour EWMA hourly volatility at the current "
              "bar. A reading exists once 720 bars are loaded; the percentile needs 200 readings after that, so the "
              "picture leaves the baseline from about 920 bars.",
       maths="ℓ = ln(close). The reading at bar t is (ℓ_t−24 − ℓ_t−720) / (v_t·√696): the log return from 30 days ago to "
             "24 hours ago, divided by today's hourly volatility scaled to the 696-hour span it covers. The last 24 "
             "hours enter only through v_t. No clip, no transform. sign = +1 with the Trend defaults: a positive reading "
             "leans the picture up and fattens the upper tail.",
       trades="After a strong month (to yesterday) it puts more weight than the market on the ranges from spot to about "
              "+2σ and on the far upside and buys them; the ranges below spot are thinned and not bought. After a weak "
              "month the reverse. The reading moves slowly, so the lean persists for days; when the month's move is "
              "middling by its own history the picture is the baseline and nothing is bought."),
    _m("pivot_breakout", "Pivot Points", "Above yesterday's pivot is strength, below it is weakness",
       "Floor traders' pivot points: yesterday's high, low and close define a pivot level, with a resistance R1 above "
       "it and a support S1 below. Trading above the pivot is read as strength, below it as weakness, and the further "
       "from the pivot in units of yesterday's range the stronger the claim. A push through resistance leans bullish, "
       "a break of support bearish. The levels are fixed for a whole UTC day and jump at midnight.",
       pivot_breakout_signal, f"Support and resistance, {BOOK}, Section 3.14",
       inputs="Hourly high, low and close in price units, grouped by UTC calendar day from the bar timestamps. It uses "
              "the previous day's levels only if that day has all 24 bars; otherwise the whole day reads NaN. No "
              "volatility scaling. Readings start on the first day after a complete day; the percentile needs 200.",
       maths="For each UTC day, H, L, C are the previous complete day's high, low and last close. Pivot P = (H + L + C)/3, "
             "R1 = 2P − L, S1 = 2P − H, so R1 − S1 = H − L, yesterday's range (NaN unless > 0). The reading at every "
             "hourly bar of the day is clip((close_t − P)/(H − L), −3, 3): +1 means one full yesterday's range above the "
             "pivot; R1 itself sits at a reading of (P − L)/(H − L), which is 0.5 when yesterday closed mid-range. "
             "sign = +1 with the Trend defaults: a positive reading leans the picture up and fattens the upper tail.",
       trades="Well above yesterday's pivot, by this signal's own history, it puts more weight than the market on the "
              "ranges from spot to about +2σ and on the far upside and buys them, thinning the ranges below spot, which "
              "it never buys; well below the pivot is the mirror image. Hovering at the pivot, the reading is "
              "mid-history, the picture is the baseline and nothing is bought."),
    _m("adx_directional", "ADX Directional", "Leans with buyers or sellers, more when the trend is strong",
       "Welles Wilder's directional movement system: +DI measures how much of the recent range came from pushing to "
       "new highs, −DI how much from pushing to new lows, and ADX measures how strong the trend is regardless of "
       "direction. The reading is the buyers-minus-sellers balance weighted by trend strength, so a strong trend with "
       "buyers in control leans bullish, a strong trend with sellers in control bearish, and a trendless market says "
       "little even if one side is slightly ahead.",
       adx_directional_signal, "Directional movement index and ADX (Wilder 1978)",
       inputs="Hourly high, low and close (the close only through the true range), with Wilder's 14-hour smoothing "
              "throughout. No volatility scaling beyond the ATR. The first reading arrives at about bar 28; the 200 "
              "readings the percentile needs are inside the 400-bar minimum.",
       maths="up_t = high_t − high_t−1, dn_t = low_t−1 − low_t. +DM = up where up > dn and up > 0, else 0; −DM = dn where "
             "dn > up and dn > 0, else 0. ATR₁₄ is the exponentially weighted true range with α = 1/14 (needing 14 bars, "
             "floored at 10⁻⁹). +DI = 100·EWM(+DM, α = 1/14)/ATR₁₄, −DI likewise. DX = 100·|+DI − −DI| / (+DI + −DI), NaN "
             "when the sum is 0; ADX = EWM(DX, α = 1/14). The reading is (+DI − −DI)/100 × ADX/25: the directional "
             "balance scaled to [−1, 1], times the trend strength in units of the conventional 25 threshold. No clip. "
             "sign = +1 with the Trend defaults: a positive reading leans the picture up and fattens the upper tail.",
       trades="A strong trend with buyers ahead puts more weight than the market on the ranges from spot to about +2σ "
              "and on the far upside and buys them; the ranges below spot are thinned and not bought. Sellers ahead in a "
              "strong trend flips that. A weak ADX shrinks the reading towards zero whichever side leads, so a "
              "choppy market sits mid-history, leaves the baseline and buys nothing."),
    _m("keltner_breakout", "Keltner Breakout", "Leans with a close outside the 20-hour Keltner channel",
       "Keltner channels wrap a 20-hour exponential average of the price in bands two average true ranges wide. A "
       "close beyond a band is a volatility-adjusted breakout: the price has moved further from its average than the "
       "recent bar-to-bar range says it normally does, which trend followers take as the start of a move rather than "
       "noise. A push through the upper band leans bullish, a drop through the lower band bearish.",
       keltner_breakout_signal, "Keltner channels (Keltner 1960; ATR form after Linda Raschke)",
       inputs="Hourly high, low and close in price units: the close through a 20-hour exponential average, all three "
              "through the 20-hour average true range. No log transform and no other volatility scaling. The first "
              "reading arrives at bar 20; the 200 readings the percentile needs are inside the 400-bar minimum.",
       maths="c = close in price units; EMA(c, 20) the span-20 exponentially weighted mean (α = 2/21, needing 20 bars); "
             "ATR₂₀ the exponentially weighted true range with α = 1/20, floored at 10⁻⁹. The reading is "
             "clip((c_t − EMA(c, 20)_t) / (2·ATR₂₀,t), −3, 3): +1 exactly at the upper band, −1 at the lower band, ±3 at "
             "three band-widths out. sign = +1 with the Trend defaults: a positive reading leans the picture up and "
             "fattens the upper tail.",
       trades="A close well beyond the upper band puts more weight than the market on the ranges from spot to about "
              "+2σ and on the far upside and buys them, while thinning the ranges below spot, which it never buys; a "
              "close beyond the lower band is the mirror image. A close near the 20-hour average is mid-history, the "
              "picture is the baseline and nothing is bought."),
    _m("supertrend", "Supertrend", "Bullish while the trailing stop sits below the price",
       "Supertrend, a favourite on retail charting platforms: a trailing stop three average true ranges from the bar's "
       "mid-point that only ratchets in the trend's favour and flips to the other side when the price closes through "
       "it. While the stop is below the price the market is in an uptrend, while above it a downtrend, and the "
       "distance to the stop says how much room the trend has before it is broken. Positive in an uptrend, negative "
       "in a downtrend.",
       supertrend_signal, "Supertrend indicator (Olivier Seban); ATR after Wilder (1978)",
       inputs="Hourly high, low and close in price units, with a 10-hour average true range. The state (uptrend or "
              "downtrend) is carried from the first loaded bar, starting as an uptrend. The first reading arrives at bar "
              "10; the 200 readings the percentile needs are inside the 400-bar minimum.",
       maths="ATR₁₀ is the exponentially weighted true range with α = 1/10 (needing 10 bars). Raw bands: mid = (high + "
             "low)/2, upper = mid + 3·ATR₁₀, lower = mid − 3·ATR₁₀. Final bands ratchet: fu_t = upper_t if upper_t < fu_t−1 "
             "or close_t−1 > fu_t−1, else fu_t−1; fl_t = lower_t if lower_t > fl_t−1 or close_t−1 < fl_t−1, else fl_t−1. "
             "Direction starts at +1, flips to −1 when close_t < fl_t and back to +1 when close_t > fu_t. The active "
             "band is fl in an uptrend and fu in a downtrend, and the reading is clip((close_t − band_t)/ATR₁₀,t, −6, 6), "
             "so its sign is the trend state and its size the distance to the stop in ATRs. sign = +1 with the Trend "
             "defaults: a positive reading leans the picture up and fattens the upper tail.",
       trades="Deep in an uptrend, with the price far above its stop by this signal's own history, it puts more weight "
              "than the market on the ranges from spot to about +2σ and on the far upside and buys them; the ranges "
              "below spot are thinned and not bought. Deep in a downtrend, the reverse. Just after a flip, or hugging "
              "the stop, the reading is small and near mid-history: baseline picture, no trades."),
    _m("parabolic_sar", "Parabolic SAR", "Rides the trend until the accelerating stop is crossed",
       "Wilder's parabolic stop-and-reverse trails the price with a stop that starts slowly and accelerates each time "
       "the trend makes a new extreme, so a mature trend is held on a tight leash and an early one is given room. When "
       "the price crosses the stop the system reverses. The reading is how far the close sits from the stop, in "
       "average true ranges: a price riding well above its stop leans bullish, one pinned below it bearish.",
       parabolic_sar_signal, "Parabolic SAR (Wilder 1978)",
       inputs="Hourly high and low in price units for the stop, close and the 14-hour average true range for the "
              "reading. The stop is run from the first loaded bar, assumed to start in an uptrend. The first reading "
              "arrives at bar 14; the 200 readings the percentile needs are inside the 400-bar minimum.",
       maths="Wilder's SAR with acceleration step 0.02 and cap 0.2, starting long with SAR = low₀, extreme point "
             "EP = high₀, AF = 0.02. Each bar: SAR ← SAR + AF·(EP − SAR); in an uptrend SAR is capped at min(low_t−1, "
             "low_t−2) and if low_t < SAR the system reverses (SAR ← EP, EP ← low_t, AF ← 0.02), else a new high sets "
             "EP ← high_t and AF ← min(AF + 0.02, 0.2); in a downtrend the mirror with highs. The reading is "
             "clip((close_t − SAR_t)/ATR₁₄,t, −8, 8), ATR₁₄ the exponentially weighted true range with α = 1/14. sign = +1 "
             "with the Trend defaults: a positive reading leans the picture up and fattens the upper tail.",
       trades="With the close far above an accelerating stop, by this signal's own history, it puts more weight than "
              "the market on the ranges from spot to about +2σ and on the far upside and buys them, thinning the ranges "
              "below spot, which it never buys; far below the stop is the mirror image. Right after a reversal the stop "
              "sits at the old extreme, the reading is small, the picture is near the baseline and nothing is bought."),
    _m("ichimoku_cloud", "Ichimoku Cloud", "Bullish above the cloud, bearish below, quiet inside it",
       "The Ichimoku cloud read at a glance on hourly bars with the standard 9-26-52 settings. The cloud is built from "
       "mid-points of past highs and lows and projected 26 hours forward, so today's cloud was drawn from prices a "
       "day ago. Price above the cloud with the conversion line above the base line is the classic bullish set-up; "
       "below the cloud with the lines inverted is bearish; inside the cloud, or with the lines disagreeing, the "
       "model has no opinion at all.",
       ichimoku_signal, "Ichimoku Kinko Hyo (Goichi Hosoda, 1969)",
       inputs="Hourly high, low and close in price units over rolling windows of 9, 26 and 52 hours, shifted 26 hours, "
              "plus the 26-hour average true range. The lagging (chikou) span is not used. The first reading arrives "
              "at bar 78 (52 + 26), zeros included; the 200 readings the percentile needs are inside the 400-bar minimum.",
       maths="mid(n) = (max high over n bars + min low over n bars)/2. Tenkan = mid(9), kijun = mid(26), "
             "span A_t = ((tenkan + kijun)/2)_t−26, span B_t = mid(52)_t−26; top = max(A, B), bottom = min(A, B). ATR₂₆ is "
             "the exponentially weighted true range with α = 1/26. above_t = (close − top)/ATR₂₆ if close > top and tenkan "
             "> kijun, else 0; below_t = (close − bottom)/ATR₂₆ if close < bottom and tenkan < kijun, else 0. The reading "
             "is clip(above + below, −6, 6), NaN until span B exists. sign = +1 with the Trend defaults: a positive reading "
             "leans the picture up and fattens the upper tail. A 0 ranks at the middle of the block of past zeros.",
       trades="Far above the cloud with the lines aligned it puts more weight than the market on the ranges from spot "
              "to about +2σ and on the far upside and buys them; the ranges below spot are thinned and not bought. Far "
              "below the cloud with the lines inverted, the reverse. Inside the cloud or with the lines disagreeing "
              "the reading is exactly 0, near-neutral in its own history: the picture stays close to the baseline and "
              "the bot buys nothing."),
    _m("aroon_oscillator", "Aroon Oscillator", "Leans towards whichever is fresher: the 25-hour high or low",
       "Tushar Chande's Aroon oscillator asks a timing question rather than a size question: how recently did Bitcoin "
       "make its 25-hour high, and how recently its 25-hour low? Fresh highs with stale lows mean an uptrend, fresh "
       "lows with stale highs a downtrend, and a high and a low of similar age mean no trend. The picture leans towards "
       "the fresher extreme, regardless of how large the move was.",
       aroon_signal, "Aroon indicator (Chande 1995)",
       inputs="Hourly high and low only, over a window of 26 bars (this bar and the 25 before it). No volatility "
              "scaling and no price units at all: the reading is a count of bars. The first reading arrives at bar 26 "
              "(NaN if any bar in the window is missing); the 200 readings the percentile needs are inside the 400-bar "
              "minimum.",
       maths="With n = 25, h_t = bars since the highest high of the last n + 1 bars (0 if it is the current bar, 25 if "
             "the oldest; on a tie the oldest wins) and l_t likewise for the lowest low. Aroon up = (n − h_t)/n, Aroon "
             "down = (n − l_t)/n, and the reading is their difference (l_t − h_t)/25 ∈ [−1, 1], taking 51 discrete "
             "values: +1 when the high is this bar and the low 25 bars old. sign = +1 with the Trend defaults: a "
             "positive reading leans the picture up and fattens the upper tail.",
       trades="A fresh 25-hour high with a stale low puts more weight than the market on the ranges from spot to about "
              "+2σ and on the far upside and buys them, thinning the ranges below spot, which it never buys; a fresh "
              "low with a stale high is the mirror image. Because the reading is a coarse count, the lean moves in "
              "steps; when high and low are of similar age it is mid-history, the picture is the baseline and nothing "
              "is bought."),
    _m("heikin_ashi_trend", "Heikin-Ashi Run", "Counts the run of same-coloured smoothed candles",
       "Heikin-Ashi candles average each bar with the previous one, which turns a noisy chart into long runs of "
       "same-coloured candles. The model counts the current run: six green candles in a row is a steadier uptrend "
       "than one, and a run of red candles is a downtrend. The count is squashed so very long runs do not dominate, "
       "and a single opposite-coloured candle resets it, so the lean can vanish in one hour.",
       heikin_ashi_signal, "Heikin-Ashi candlesticks (Valcu 2004, Technical Analysis of Stocks & Commodities)",
       inputs="Hourly open, high, low and close in price units, recomputed as Heikin-Ashi candles from the first loaded "
              "bar (so the candle colours depend slightly on where the history starts). No volatility scaling. A reading "
              "exists from the first bar; the percentile needs 200 of them, inside the 400-bar minimum.",
       maths="HA close = (open + high + low + close)/4; HA open_t = (HA open_t−1 + HA close_t−1)/2 with HA open₀ = "
             "(open₀ + close₀)/2. Colour = sign(HA close − HA open). The run r_t is +k after k consecutive green candles, "
             "−k after k red ones, and 0 on an exact tie. The reading is tanh(r_t/6): a run of 3 gives 0.46, 6 gives "
             "0.76, 12 gives 0.96, so it saturates near 1 after about two days of one colour. sign = +1 with the Trend "
             "defaults: a positive reading leans the picture up and fattens the upper tail.",
       trades="A long green run puts more weight than the market on the ranges from spot to about +2σ and on the far "
              "upside and buys them, thinning the ranges below spot, which it never buys; a long red run is the mirror "
              "image. Runs of one or two candles either way sit near the middle of the reading's history, so the picture "
              "is close to the baseline and nothing is bought; the lean disappears the hour the colour changes."),
    _m("obv_trend", "On-Balance Volume", "Follows the volume: buying volume bullish, selling bearish",
       "On-balance volume adds the hour's volume when the price closes up and subtracts it when it closes down, so it "
       "climbs when volume arrives on up-moves and sinks when it arrives on down-moves. Joe Granville's idea is that "
       "volume leads price: a move backed by volume is more likely to continue. The model reads the last 3 days of "
       "volume flow against the last 30 days of such readings, so unusually heavy buying volume leans bullish and "
       "unusually heavy selling volume bearish.",
       obv_signal, "On-balance volume (Granville 1963)",
       inputs="Hourly close and volume (negative volumes are floored at 0). The 72-hour (3-day) OBV change is "
              "standardised over a rolling 720-hour (30-day) window that needs 180 values, so the first reading arrives "
              "at bar 252 and the picture can leave the baseline from about bar 452, past the 400-bar minimum.",
       maths="OBV_t = Σ_{s ≤ t} sign(close_s − close_s−1)·max(volume_s, 0), with the first bar's sign taken as 0 and a "
             "flat close adding nothing. d_t = OBV_t − OBV_t−72. The reading is the rolling z-score of d over 720 bars: "
             "(d_t − mean(d, 720)) / std(d, 720), needing 180 of the 720 values and NaN when the standard deviation is "
             "≤ 10⁻¹². It is not scaled by price volatility: the volume units cancel in the z-score. sign = +1 with "
             "the Trend defaults: a positive reading leans the picture up and fattens the upper tail.",
       trades="Three days of unusually heavy buying volume, against the last month, puts more weight than the market on "
              "the ranges from spot to about +2σ and on the far upside and buys them; the ranges below spot are thinned "
              "and not bought. Unusually heavy selling volume is the mirror image. Volume flow near its 30-day average "
              "reads near zero, the picture is the baseline and nothing is bought."),
    _m("variance_ratio_trend", "Variance Ratio", "Follows the last day if moves persist, fades it if they revert",
       "Lo and MacKinlay's variance ratio tells trending markets from mean-reverting ones: if 24-hour moves are bigger "
       "than 24 independent one-hour moves would produce, hourly moves have been persisting; if smaller, they have "
       "been reverting. This model follows the last day's direction when the past two weeks look persistent and fades "
       "it when they look mean-reverting, so it can be a trend follower one week and a contrarian the next.",
       variance_ratio_signal, "Variance ratio test (Lo and MacKinlay 1988)",
       inputs="Hourly closes only: one-hour and overlapping 24-hour log returns over a rolling 336-hour (14-day) window "
              "that needs 168 values, plus the 168-hour EWMA hourly volatility. The first reading arrives at bar 192; "
              "the 200 readings the percentile needs arrive by bar 392, inside the 400-bar minimum.",
       maths="ℓ = ln(close), r₁ = ℓ_t − ℓ_t−1, r₂₄ = ℓ_t − ℓ_t−24. var₁ = mean of r₁² and var₂₄ = mean of r₂₄² over the "
             "last 336 bars (uncentred second moments, overlapping 24-hour returns, at least 168 values). "
             "VR = var₂₄ / (24·var₁), NaN when var₁ is 0; under a random walk VR = 1. Momentum m_t = r₂₄ / (v_t·√24). "
             "The reading is clip(VR − 1, −1, 2) × tanh(m_t): positive when moves persist and the last day was up, or "
             "when moves revert and the last day was down; the clip lets a persistent regime count up to twice as much "
             "as a reverting one. sign = +1 with the Trend defaults: a positive reading leans the picture up.",
       trades="A persistent fortnight plus a strong up-day, or a reverting fortnight plus a strong down-day, puts more "
              "weight than the market on the ranges from spot to about +2σ and on the far upside and buys them, "
              "thinning the ranges below spot, which it never buys; the opposite combinations mirror that. A ratio near "
              "1, or a flat last day, reads near zero and mid-history: baseline picture, no trades."),
]
