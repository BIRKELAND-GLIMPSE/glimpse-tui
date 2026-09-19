"""Mean-reversion pictures, and the two factor models that need nothing but candles.

The lab's `directional.py` (RSI, Bollinger, VWAP), `zoo_reversion.py`, `range_box_paths` from `zoo_shapes.py` and
`low_vol_anomaly` / `skewness_premium` from `zoo_calendar.py`. Every reversion signal is already written contrarian
(a positive value means "the price is expected to come back up"), so every model here uses sign = +1, as in the lab.
"""
from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pandas as pd

from .core import Ctx, Model, base_pdf, cached, conviction_picture, from_samples, rng_for, to_bins
from .signals import daily_levels, hourly_vol, log_close, zscore

BOOK = "Kakushadze and Serur, 151 Trading Strategies (2018)"
REVERSION = "Mean reversion"
FACTOR = "Factor & carry"
HVOL = (
    "v is the hourly volatility from signals.hourly_vol: the exponentially weighted standard deviation of hourly log "
    "returns with span 168 hours (α = 2/169), at least 48 returns, floored at 10⁻⁶"
)
REV_MATHS = (
    " sign = +1, family Mean reversion with no overrides: conviction 0.6, shape conviction 0.45, snap timing, revert "
    "shape."
)
FACTOR_MATHS = (
    " sign = +1, family Factor & carry with no overrides: conviction 0.6, shape conviction 0.45, ride timing, trend "
    "shape (the tail on the signal's side is fattened at 0.45·|s|)."
)
REV_TRADES = (
    " At its highest readings it puts more weight than the market on the ranges from about a quarter σ above spot "
    "upwards, most between 0.5σ and 3σ, and buys those; it thins every range below and buys none. The lean is full "
    "within a day of the close and fades as √(24/h) beyond (half at four days). The lowest readings mirror this; a "
    "mid-history reading leaves the baseline and buys nothing."
)
FACTOR_TRADES = (
    " It puts more weight than the market on every range above spot, increasingly towards the far upside, and buys "
    "those; it strips the ranges below spot, the far downside almost entirely, and buys none, at full strength at "
    "every horizon. The lowest readings mirror this; a mid-history reading leaves the baseline and buys nothing."
)
PERCENTILE = " The strength percentile needs 200 finite readings on top of that."


def _close(bars: pd.DataFrame) -> pd.Series:
    return bars["close"].astype(float)


def stepped(bars: pd.DataFrame, fn: Callable[[pd.DataFrame], float], *, step: int, lookback: int) -> pd.Series:
    """The lab's `DriftOverlayModel.stepped`: a slow signal evaluated every `step` hours on a fixed UTC grid from the
    `lookback` bars ending at each origin, forward-filled to the bars in between."""
    idx = pd.DatetimeIndex(bars.index)
    hours = (idx.as_unit("s").asi8 // 3600).astype(np.int64)
    out = np.full(len(idx), np.nan)
    for i in np.nonzero((hours % step == 0) & (np.arange(len(idx)) >= lookback - 1))[0]:
        try:
            out[i] = float(fn(bars.iloc[i - lookback + 1 : i + 1]))
        except Exception:  # noqa: BLE001 - a failed fit at one origin is a missing value
            out[i] = np.nan
    return pd.Series(out, index=idx).ffill(limit=step - 1)


# ── signals ─────────────────────────────────────────────────

def rsi_signal(bars: pd.DataFrame) -> pd.Series:
    d = _close(bars).diff()
    up = d.clip(lower=0).ewm(alpha=1 / 14, min_periods=14).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / 14, min_periods=14).mean()
    rsi = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    return 50.0 - rsi


def bollinger_signal(bars: pd.DataFrame) -> pd.Series:
    lp = log_close(bars)
    mean = lp.rolling(20, min_periods=20).mean()
    sd = lp.rolling(20, min_periods=20).std().clip(lower=1e-9)
    return -(lp - mean) / (2 * sd)


def vwap_signal(bars: pd.DataFrame) -> pd.Series:
    c = _close(bars)
    v = bars["volume"].astype(float).clip(lower=0)
    pv = (c * v).rolling(168, min_periods=48).sum()
    vv = v.rolling(168, min_periods=48).sum()
    vwap = (pv / vv).where(vv > 0, c.rolling(168, min_periods=48).mean())
    return -np.log(c / vwap) / (hourly_vol(bars) * np.sqrt(168))


def short_term_reversal_signal(bars: pd.DataFrame, lookback: int = 24) -> pd.Series:
    return -log_close(bars).diff(lookback) / (hourly_vol(bars) * np.sqrt(lookback))


def channel_signal(bars: pd.DataFrame, channel_hours: int = 72) -> pd.Series:
    n = channel_hours
    hi = bars["high"].astype(float).rolling(n, min_periods=n).max()
    lo = bars["low"].astype(float).rolling(n, min_periods=n).min()
    half = ((hi - lo) / 2).where(lambda x: x > 0)
    return -((_close(bars) - (hi + lo) / 2) / half).clip(-1, 1)


def support_resistance_signal(bars: pd.DataFrame) -> pd.Series:
    lv = daily_levels(bars)
    c = _close(bars)
    width = (lv["R1"] - lv["S1"]).where(lambda x: x > 0)
    near_s = (1 - (c - lv["S1"]).abs() / (0.25 * width)).clip(lower=0)
    near_r = (1 - (c - lv["R1"]).abs() / (0.25 * width)).clip(lower=0)
    return near_s - near_r


def stochastic_signal(bars: pd.DataFrame) -> pd.Series:
    hi = bars["high"].astype(float).rolling(14, min_periods=14).max()
    lo = bars["low"].astype(float).rolling(14, min_periods=14).min()
    k = 100 * (_close(bars) - lo) / (hi - lo).where(lambda x: x > 0)
    return (50.0 - k.rolling(3, min_periods=3).mean()) / 50.0


def cci_signal(bars: pd.DataFrame) -> pd.Series:
    tp = (bars["high"].astype(float) + bars["low"].astype(float) + _close(bars)) / 3
    m = tp.rolling(20, min_periods=20).mean()
    mad = (tp - m).abs().rolling(20, min_periods=20).mean()
    cci = (tp - m) / (0.015 * mad.where(lambda x: x > 0))
    return -(cci / 100.0).clip(-4, 4)


def ou_level_signal(bars: pd.DataFrame, fit_hours: int = 336) -> pd.Series:
    n = fit_hours
    x = log_close(bars)
    y, xl = x, x.shift(1)  # p_t on p_{t-1}
    sx, sy = xl.rolling(n, min_periods=n).sum(), y.rolling(n, min_periods=n).sum()
    sxx = (xl * xl).rolling(n, min_periods=n).sum()
    sxy = (xl * y).rolling(n, min_periods=n).sum()
    phi = (sxy - sx * sy / n) / (sxx - sx * sx / n)
    a = (sy - phi * sx) / n
    pull = (1.0 - phi).clip(lower=0.0)
    level = a / (1.0 - phi).where(lambda v: v > 1e-6)
    sig = pull * (level - x) / hourly_vol(bars)
    return sig.where(phi < 1.0, 0.0).clip(-5, 5)


def volume_poc_signal(bars: pd.DataFrame) -> pd.Series:
    def poc(window: pd.DataFrame) -> float:
        lp = np.log(window["close"].to_numpy(dtype=float))
        vol = window["volume"].to_numpy(dtype=float).clip(min=0)
        if vol.sum() <= 0:
            vol = np.ones_like(lp)
        hist, edges = np.histogram(lp, bins=40, weights=vol)
        k = int(np.argmax(hist))
        return float((edges[k] + edges[k + 1]) / 2 - lp[-1])

    gap = stepped(bars, poc, step=3, lookback=336)
    return gap / (hourly_vol(bars) * np.sqrt(24))


def round_number_signal(bars: pd.DataFrame, level_usd: float = 1000.0) -> pd.Series:
    c = _close(bars)
    nearest = np.round(c / level_usd) * level_usd
    gap = (nearest - c) / c  # log-like distance towards the nearest level
    weight = (1 - (nearest - c).abs() / (level_usd / 2)).clip(lower=0)
    return gap * weight / (hourly_vol(bars) * np.sqrt(24))


def low_vol_signal(bars: pd.DataFrame, rv_hours: int = 168, history_hours: int = 2160) -> pd.Series:
    rv = log_close(bars).diff().rolling(rv_hours, min_periods=rv_hours).std().clip(lower=1e-9)
    return -zscore(np.log(rv), history_hours)


def skewness_signal(bars: pd.DataFrame, skew_hours: int = 720) -> pd.Series:
    return -log_close(bars).diff().rolling(skew_hours, min_periods=skew_hours).skew()


def _signal_model(model_id: str, build: Callable[[pd.DataFrame], pd.Series], family: str) -> Callable[[Ctx], np.ndarray]:
    def fn(ctx: Ctx) -> np.ndarray:
        signal = cached(ctx, f"{model_id}:signal", lambda: build(ctx.bars).to_numpy(dtype=float))
        return conviction_picture(ctx, signal, sign=1, family=family)

    fn.__name__ = model_id
    return fn


# ── range box (Ornstein-Uhlenbeck paths) ────────────────────

def fit_range_box(bars: pd.DataFrame, fit_hours: int = 720, df_critical: float = -3.43) -> dict[str, float | bool]:
    """The lab's `RangeBoxPaths.fit_structure`: a Dickey-Fuller regression of the hourly change on the level."""
    p = np.log(bars["close"].to_numpy(dtype=float))[-fit_hours:]
    x, dy = p[:-1], np.diff(p)
    xm = x.mean()
    sxx = float(np.dot(x - xm, x - xm))
    b = float(np.dot(x - xm, dy - dy.mean()) / sxx) if sxx > 0 else 0.0
    a = float(dy.mean() - b * xm)
    resid = dy - a - b * x
    se = float(np.sqrt(np.dot(resid, resid) / max(resid.size - 2, 1) / sxx)) if sxx > 0 else np.inf
    t = b / se if se > 0 else 0.0
    kappa = -b if (t < df_critical and b < 0) else 0.0
    shrunk = kappa * max(0.0, 1.0 - 1.0 / (t * t)) if kappa > 0 else 0.0  # James-Stein, c = 1, after the gate
    level = a / kappa if kappa > 0 else float(p[-1])
    return {"active": bool(shrunk > 0 and np.isfinite(shrunk) and np.isfinite(level)), "df_t": float(t), "kappa": float(shrunk),
            "log_level": float(level)}


def _step_fractions(now: float, end: float) -> np.ndarray:
    """Length of each step of `Scale.step_variances(now, end)`, in hours."""
    if end <= now:
        return np.array([1e-6])
    out, t = [], now
    while t < end - 1e-6:
        nxt = min((t // 3600 + 1) * 3600, end)
        out.append((nxt - t) / 3600)
        t = nxt
    return np.array(out)


def range_box_paths(ctx: Ctx, n_paths: int = 4000) -> np.ndarray:
    s = cached(ctx, "range_box_paths:structure", lambda: fit_range_box(ctx.bars))
    if not s["active"]:
        return to_bins(ctx, base_pdf(ctx))
    rng = rng_for(ctx, "range_box_paths")
    sd = np.sqrt(ctx.scale.step_variances(ctx.now, ctx.end))
    frac = _step_fractions(ctx.now, ctx.end)
    pool = ctx.scale.z_pool / max(float(np.std(ctx.scale.z_pool)), 1e-12)
    inc = pool[rng.integers(0, pool.size, size=(n_paths, sd.size))] * sd
    reach = 5.0 * float(np.sqrt(ctx.scale.s2_next)) * np.sqrt(168.0)
    target = float(np.clip(s["log_level"] - ctx.log_spot, -reach, reach))
    x = np.zeros(n_paths)
    for j in range(sd.size):
        x = x - s["kappa"] * frac[j] * (x - target) + inc[:, j]
    return from_samples(ctx, x)


# ── catalogue ───────────────────────────────────────────────

_REV_FACTORS = ("direction", "shape", "timing")
_FACTOR_FACTORS = ("direction", "tails")


def _rev(model_id: str, name: str, blurb: str, description: str, build: Callable[[pd.DataFrame], pd.Series], reference: str,
         *, inputs: str, maths: str, trades: str) -> Model:
    return Model(model_id, name, REVERSION, blurb, description, _signal_model(model_id, build, REVERSION),
                 factors=_REV_FACTORS, reference=reference, inputs=inputs + PERCENTILE, maths=maths + REV_MATHS,
                 trades=trades + REV_TRADES, pipeline="conviction")


def _factor(model_id: str, name: str, blurb: str, description: str, build: Callable[[pd.DataFrame], pd.Series], reference: str,
            *, inputs: str, maths: str, trades: str) -> Model:
    return Model(model_id, name, FACTOR, blurb, description, _signal_model(model_id, build, FACTOR),
                 factors=_FACTOR_FACTORS, reference=reference, inputs=inputs + PERCENTILE, maths=maths + FACTOR_MATHS,
                 trades=trades + FACTOR_TRADES, pipeline="conviction")


MODELS = [
    _rev("rsi_reversion", "RSI Reversion", "Oversold on the 14-hour RSI leans up, overbought leans down",
         "Wilder's relative strength index compares the average size of recent up-hours with recent down-hours. Read as "
         "a contrarian: when nearly every recent hour has been a down-hour the market is called oversold and expected to "
         "bounce, when nearly every hour has been an up-hour it is overbought and expected to give some back. The lean "
         "grows with how lopsided the last day or two of hours have been, and vanishes when ups and downs balance.",
         rsi_signal, "Relative strength index (Wilder 1978) as a mean-reversion signal",
         inputs="Hourly closes only: dollar changes from one close to the next, exponentially smoothed with α = 1/14 "
                "(an effective memory of about 14 hours). It says something from the 15th bar onwards.",
         maths="With d_t = C_t − C_t−1 in dollars, U = the exponentially weighted mean of max(d, 0) and D = that of "
               "max(−d, 0), both with α = 1/14 (Wilder's smoothing, pandas' adjusted weights, at least 14 differences), "
               "RSI = 100 − 100/(1 + U/D), undefined while D = 0. The signal is 50 − RSI, between −50 and +50, not "
               "volatility-scaled. A positive reading (oversold: down-hours have outweighed up-hours) moves the centre "
               "up.",
         trades="After a lopsided run of down-hours it leans up, after a run of up-hours down."),
    _rev("bollinger_reversion", "Bollinger Reversion", "Fades the price when it stretches too far from average",
         "Bollinger bands draw a moving average with a band two standard deviations either side, and the contrarian "
         "reading is that a price pressing on the upper band has stretched too far and will come back to the average, "
         "while one on the lower band is due a bounce. Here the average and the bands are taken over the last 20 hours "
         "of log prices, so it is a same-day reversion signal that renews itself as the average catches up.",
         bollinger_signal, "Bollinger bands (Bollinger 2001) as a mean-reversion signal",
         inputs="Hourly closes only, as log prices, with a 20-hour rolling mean and standard deviation. It says "
                "something from the 20th bar onwards.",
         maths="With ℓ = ln C, m the 20-hour rolling mean of ℓ and σ₂₀ its 20-hour rolling sample standard deviation "
               "(floored at 10⁻⁹), the signal is −(ℓ_t − m_t)/(2·σ₂₀): minus the price's distance from its average in "
               "band half-widths, so −1 sits on the upper band and +1 on the lower, with no clip. A positive reading "
               "(price below its 20-hour average) moves the centre up.",
         trades="With the price below its 20-hour average it leans up, above it down."),
    _rev("vwap_reversion", "VWAP Reversion", "Leans back towards the 7-day volume-weighted average price",
         "Mean reversion to the volume-weighted average price of the last week: the price at which the most money "
         "actually changed hands, which intraday traders treat as fair value. A price well above it is expected to drift "
         "back down, one well below it to recover, and the gap is measured in units of a one-week volatility so that a "
         "wide gap in a calm week counts for more than the same gap in a wild one.",
         vwap_signal, "Anchored VWAP reversion; Ornstein-Uhlenbeck spread trading (Avellaneda and Lee 2010)",
         inputs="Hourly closes and volumes over the last 168 hours (7 days), at least 48 of them, and the hourly "
                "volatility (48 returns). It says something from the 49th bar onwards; where the week's volume sums to "
                "zero a 168-hour simple mean of the close stands in for the VWAP.",
         maths=f"With C the close and V the volume (negatives set to 0), VWAP_t = Σ C·V / Σ V over the last 168 hours "
               f"(at least 48). The signal is −ln(C_t / VWAP_t) / (v_t·√168), where {HVOL}: minus the log gap to the VWAP "
               "in units of a one-week σ, not clipped. A positive reading (price under the VWAP) moves the centre up.",
         trades="With the price under the week's VWAP it leans up, above it down."),
    _rev("short_term_reversal", "Short-Term Reversal", "Fades the last 24 hours: a sharp drop leans up, a rally down",
         "Short-term reversal, the contrarian's oldest rule: a sharp one-day move tends to give some of itself back, "
         "because liquidity providers who absorbed the move are paid to take the other side. The last day's return is "
         "measured in units of a one-day volatility, so what counts is not the size of the move in dollars but how "
         "unusual it is for the market as it is now. A sharp drop leans the picture bullish, a sharp rally bearish.",
         short_term_reversal_signal,
         f"Mean-reversion – single cluster, {BOOK}, Section 3.9; Contrarian trading, Section 10.3; Lehmann (1990)",
         inputs="Hourly closes only: the log return over the last 24 hours and the hourly volatility (48 returns). It "
                "says something from the 49th bar onwards.",
         maths=f"With ℓ = ln C, the signal is −(ℓ_t − ℓ_t−24) / (v_t·√24), where {HVOL}: minus the one-day log return in "
               "units of a one-day σ, not clipped. A positive reading (the last day fell) moves the centre up.",
         trades="After a down day it leans up, after an up day down."),
    _rev("channel_reversion", "Channel Reversion", "Buys the floor and sells the ceiling of the 3-day range",
         "Trade the range, not the breakout: inside a 3-day high-low channel, buy near the floor and sell near the "
         "ceiling on the belief that the range holds more often than it breaks. The reading is where the last close "
         "sits between the channel's floor and ceiling, so a price on support leans the picture bullish and one pressing "
         "on resistance leans it bearish. The mirror image of the Donchian breakout model.",
         channel_signal, f"Channel, {BOOK}, Section 3.15",
         inputs="Hourly highs, lows and closes over the last 72 hours (3 days). It says something from the 72nd bar "
                "onwards, and nothing while the channel has zero height.",
         maths="H₇₂ = rolling max of the high and L₇₂ = rolling min of the low over 72 hours, mid = (H₇₂ + L₇₂)/2, "
               "half = (H₇₂ − L₇₂)/2 (undefined when zero). The signal is −clip((C_t − mid)/half, −1, 1): +1 with the "
               "close on the channel floor, −1 on the ceiling, 0 in the middle. A positive reading (lower half of the "
               "channel) moves the centre up.",
         trades="With the close in the lower half of the 3-day range it leans up, in the upper half down."),
    _rev("support_resistance_bounce", "Support & Resistance Bounce", "Expects a bounce at yesterday's pivot support or resistance",
         "Pivot-point support and resistance as a bounce trade: yesterday's high, low and close give the floor trader's "
         "pivot, today's support (S1) and resistance (R1). Near support the model expects buyers to step in and leans "
         "bullish; near resistance it expects sellers and leans bearish; more than a quarter of yesterday's range from "
         "both it says nothing. The reading grows as the price approaches a level and peaks on it.",
         support_resistance_signal, f"Support and resistance, {BOOK}, Section 3.14",
         inputs="Hourly highs, lows and closes, folded into UTC days: the previous complete day (24 bars) sets the "
                "levels for every hour of today. It says something from the first hour after the first complete UTC "
                "day on the loaded candles (between the 25th and the 48th bar), and nothing on a day whose predecessor "
                "is incomplete.",
         maths="From the previous complete UTC day's high H, low L and close C: P = (H + L + C)/3, R1 = 2P − L, "
               "S1 = 2P − H, so R1 − S1 = H − L, yesterday's range. near_S = max(0, 1 − |C_t − S1| / (0.25·(R1 − S1))) "
               "and near_R the same for R1; the signal is near_S − near_R: +1 with the close on S1, −1 on R1, 0 once "
               "the close is more than a quarter of yesterday's range from both. Zeros dominate the history, and the "
               "mid-rank percentile keeps a zero reading near the middle. A positive reading (near support) moves the "
               "centre up.",
         trades="Within a quarter of yesterday's range of S1 it leans up, of R1 down, and in between it sits close to "
                "the baseline."),
    _rev("stochastic_reversion", "Stochastic Reversion", "Oversold in the 14-hour range leans up, overbought leans down",
         "George Lane's stochastic oscillator asks where the last close landed inside the recent high-low range. Near "
         "the bottom of the 14-hour range the market is called oversold, near the top overbought, and the contrarian "
         "reading expects the close to migrate back towards the middle of the range. The position is smoothed over "
         "three hours so a single spike does not flip the lean.",
         stochastic_signal, "Stochastic oscillator (Lane 1984)",
         inputs="Hourly highs, lows and closes over the last 14 hours, with a 3-hour smoothing. It says something "
                "from the 16th bar onwards, and nothing while the 14-hour range has zero height.",
         maths="H₁₄ and L₁₄ are the rolling max high and min low over 14 hours; %K = 100·(C_t − L₁₄)/(H₁₄ − L₁₄), "
               "undefined when the range is zero. The signal is (50 − mean of %K over the last 3 hours)/50, between −1 "
               "(close pinned at the top of the range) and +1 (at the bottom). A positive reading (lower half of the "
               "range) moves the centre up.",
         trades="With the smoothed close in the lower half of the 14-hour range it leans up, in the upper half down."),
    _rev("cci_reversion", "CCI Reversion", "Fades a typical price far from its 20-hour average",
         "Donald Lambert's commodity channel index measures how far the typical price (the mean of high, low and "
         "close) has strayed from its 20-hour average, in units of its own mean absolute deviation; the 0.015 scaling "
         "was chosen so that most readings fall between −100 and +100 and anything beyond is unusual. Played as a "
         "contrarian: a deeply negative CCI leans the picture bullish, a strongly positive one bearish.",
         cci_signal, "Commodity channel index (Lambert 1980)",
         inputs="Hourly highs, lows and closes: a 20-hour mean of the typical price and a 20-hour mean of its absolute "
                "deviation. It says something from the 39th bar onwards, and nothing while the deviation is zero.",
         maths="Typical price TP = (H + L + C)/3, m = its 20-hour rolling mean, MAD = the 20-hour rolling mean of "
               "|TP − m| (each hour's deviation is taken from that hour's own 20-hour mean, not from the current one); "
               "CCI = (TP_t − m_t)/(0.015·MAD_t), undefined while MAD = 0. The signal is −clip(CCI/100, −4, 4), so ±4 "
               "means a CCI of ∓400 or beyond. A positive reading (typical price below its average) moves the centre up.",
         trades="With the typical price below its 20-hour average it leans up, above it down."),
    _rev("ou_level_reversion", "OU Reversion", "Fits the level the price is being pulled back to",
         "An Ornstein-Uhlenbeck reading of the last two weeks: if each hour's log price is a fraction of the way back "
         "from the last one towards some fixed level, a first-order autoregression recovers both that level and the "
         "speed of the pull. The reading is the pull the fit expects over the next hour, in units of volatility, so it "
         "is large when the price is far from the level and the pull is strong, and zero when the fit finds no pull "
         "and the price behaves like a random walk. Below the fitted level leans bullish, above it bearish.",
         ou_level_signal, "Ornstein-Uhlenbeck process (Uhlenbeck and Ornstein 1930); Avellaneda and Lee (2010)",
         inputs="Hourly closes only: a rolling 336-hour (14-day) regression of the log price on its previous hour, and "
                "the hourly volatility (48 returns). The fit exists from the 337th bar; before that the reading is 0, "
                "not missing, and those zeros stay in the history the percentile ranks against.",
         maths="Over a rolling window of n = 336 hours of ℓ = ln C, OLS of ℓ_t on ℓ_t−1 by rolling sums gives the slope "
               "φ = (Σxy − Σx·Σy/n)/(Σx² − (Σx)²/n) and intercept a = (Σy − φ·Σx)/n. The level is a/(1 − φ) (undefined "
               f"when 1 − φ ≤ 10⁻⁶) and the hourly pull max(1 − φ, 0). The signal is pull·(level − ℓ_t)/v_t, where {HVOL}, "
               "set to 0 wherever φ ≥ 1 or the fit is missing, then clipped to ±5: the expected one-hour move towards "
               "the level in hourly σ. A positive reading (price below the fitted level) moves the centre up.",
         trades="With a fitted pull and the price below the level it leans up, above it down; with no pull found the "
                "reading is 0 and the picture sits near the baseline."),
    _rev("volume_poc_magnet", "Volume POC Magnet", "Pulled towards the price where most volume traded in two weeks",
         "Volume profile traders watch the point of control: the price level where the most volume changed hands over "
         "the last two weeks, which many participants treat as fair value and which the price is expected to revisit. "
         "The model builds that profile from hourly closes and volumes every three hours and treats the point of "
         "control as a magnet: below it the picture leans bullish, above it bearish, in proportion to the distance in "
         "units of a one-day volatility.",
         volume_poc_signal, "Market Profile and volume-at-price (Steidlmayer, CBOT 1985)",
         inputs="Hourly closes and volumes over the last 336 hours (14 days), re-profiled at UTC hours 0, 3, 6, …, 21 "
                "and held between, plus the hourly volatility (48 returns). It says something from the first 3-hour "
                "origin at or after the 336th bar. Zero total volume falls back to an equal-weight profile.",
         maths="At every bar whose UTC hour is a multiple of 3, take the last 336 bars, bin their ln C into 40 "
               "equal-width bins between the window's lowest and highest log close weighting each bar by its volume "
               "(negatives set to 0; equal weights when the total is 0), and set POC = the midpoint of the fullest bin. "
               "gap = POC − ℓ_t at that origin, forward-filled for up to 2 hours. The signal is gap/(v_t·√24), where "
               f"{HVOL}: the distance to the point of control in one-day σ, not clipped. A positive reading (price "
               "below the POC) moves the centre up.",
         trades="With the price below the two-week point of control it leans up, above it down."),
    _rev("round_number_magnet", "Round Number Magnet", "Drifts towards the nearest $1,000 level",
         "Round numbers pull prices: resting orders and stops cluster at levels like $110,000 and $111,000, so the "
         "price often drifts towards the nearest one and pauses there. The reading points towards the nearest $1,000 "
         "level, is strongest a few hundred dollars from it, and fades to zero both on the level (nowhere to go) and "
         "halfway between two levels (no nearest one). A price just under a round number leans bullish, one just over "
         "it bearish.",
         round_number_signal, "Support and resistance at round numbers (Osler 2003, Journal of Finance)",
         inputs="Hourly closes only, in dollars against a fixed $1,000 grid, and the hourly volatility (48 returns). "
                "It says something from the 49th bar onwards.",
         maths="N_t is the multiple of $1,000 nearest the close C_t and d = N_t − C_t in dollars. gap = d/C_t (the "
               "fractional move to the level) and weight = max(0, 1 − |d|/500), 1 on the level and 0 halfway between "
               "levels, so gap·weight is largest $250 from a level and zero on it and midway. The signal is "
               f"gap·weight/(v_t·√24), where {HVOL}: in units of a one-day σ, not clipped. A positive reading (the "
               "nearest level is above the price) moves the centre up.",
         trades="Just below a $1,000 level it leans up, just above one down; on a level or midway between two it reads "
                "0 and sits near the baseline."),
    Model("range_box_paths", "Range Box", REVERSION, "An elastic band pulls the price back to a fitted fair level",
          "A range-bound picture: the price wanders, but an elastic band pulls it back towards a fair level. The model "
          "fits an Ornstein-Uhlenbeck process to the last 30 days of hourly log prices, which gives the level and how "
          "fast the pull works, and simulates paths that feel that pull, so the picture is narrower than a random walk "
          "and centred towards the level. The pull is only used when a Dickey-Fuller test rejects the random walk at "
          "the 1% level, and it is shrunk towards no pull by how decisively; otherwise the model shows the baseline "
          "picture.",
          range_box_paths, factors=("randomness", "tails", "shape"),
          reference="Ornstein-Uhlenbeck process (Uhlenbeck and Ornstein 1930); Dickey and Fuller (1979)",
          inputs="The last 720 hourly closes (30 days; all of them if fewer are loaded) for the Dickey-Fuller fit, and "
                 "the volatility clock's standardised residual pool, next-hour EWMA variance and per-step variances for "
                 "the simulation. No history requirement beyond the 400-bar minimum; when the test does not reject a "
                 "random walk it draws the baseline.",
          maths="Fit: with p = ln C over the last 720 hours, OLS of Δp_t = a + b·p_t−1 + ε (Dickey-Fuller with a constant, "
                "no lagged differences), t = b/se(b). If t < −3.43 (the 1% critical value) and b < 0, κ = −b per hour, "
                "shrunk James-Stein style to κ' = κ·max(0, 1 − 1/t²), and level = a/κ (unshrunk κ); otherwise the "
                "picture is the baseline. Simulation: 4,000 paths of the log move x from spot, x₀ = 0, and for each step "
                "j of Δt_j hours (partial first and last) x ← x − κ'·Δt_j·(x − target) + ε_j, where ε_j is a residual "
                "drawn with replacement from the clock's pool (unit standard deviation) times that step's baseline "
                "standard deviation, and target = level − ln spot clipped to ±5·√(168·s²_next), five one-week σ at the "
                "next-hour EWMA variance. Seeded by rng_for with id range_box_paths. No conviction, tilt or width "
                "multiplier: the pull alone sets the lean and the narrowing. A level above spot moves the centre up.",
          trades="When the fit is active the picture is narrower than the market's and its centre sits roughly the "
                 "fraction 1 − e^(−κ'·h) of the way from spot to the fitted level after h hours. It puts more weight than "
                 "the market on the ranges between spot and the level and around that centre, and buys those; it thins "
                 "both far tails and the ranges on the side away from the level, and buys none of them. When the "
                 "Dickey-Fuller test does not reject a random walk, which is most of the time, the picture is the "
                 "baseline and the bot buys nothing.",
          pipeline="paths"),
    _factor("low_vol_anomaly", "Low-Volatility Anomaly", "Calm markets lean up, turbulent ones lean down",
            "The low-volatility anomaly: across markets, calm assets have tended to earn more per unit of risk than "
            "wild ones, and turbulent spells often come with poor returns, partly because leverage-constrained investors "
            "overpay for volatility. The reading compares the past week's realised volatility with its own level over "
            "the last 90 days (at least three weeks of it). An unusually calm market leans the picture bullish, an "
            "unusually turbulent one bearish, and ordinary conditions give little lean.",
            low_vol_signal, f"Low-volatility anomaly, {BOOK}, Section 3.4",
            inputs="Hourly closes only: realised volatility over 168 hours (7 days) and its z-score over 2160 hours "
                   "(90 days) with at least 540 realised-volatility readings (22.5 days), so it says something from the "
                   "708th bar onwards; with fewer bars every reading is missing and the picture is the baseline.",
            maths="rv_t = the sample standard deviation of hourly log returns over the last 168 hours, floored at 10⁻⁹. "
                  "The signal is −(ln rv_t − m)/s, where m and s are the rolling mean and sample standard deviation of "
                  "ln rv over the last 2160 hours (at least 540; s ≤ 10⁻¹² gives no reading), not clipped. A positive "
                  "reading (volatility below its 90-day norm) moves the centre up.",
            trades="After an unusually calm week it leans up, after an unusually turbulent one down."),
    _factor("skewness_premium", "Skewness Premium", "A month of upside spikes leans down, sharp sell-offs lean up",
            "The skewness premium: assets with lottery-like returns, a history of sudden jumps up, tend to be "
            "overbought by investors who pay for the chance of another jump and earn less afterwards, while assets "
            "prone to sudden drops pay a premium for that risk. The reading is the skewness of hourly returns over the "
            "last 30 days, with its sign flipped. After a month of upside spikes the picture leans bearish; after a "
            "month of sharp sell-offs it leans bullish.",
            skewness_signal, f"Skewness premium, {BOOK}, Section 9.5",
            inputs="Hourly closes only: the sample skewness of hourly log returns over a rolling 720 hours (30 days), "
                   "so it says something from the 721st bar onwards; with fewer bars every reading is missing and the "
                   "picture is the baseline.",
            maths="The signal is −skew₇₂₀(r): the negative of the rolling sample skewness (pandas' bias-corrected third "
                  "standardised moment) of hourly log returns r_t = ln C_t − ln C_t−1 over the last 720 hours, with no "
                  "reading when the window has zero variance. Not clipped or volatility-scaled, since skewness is already "
                  "dimensionless. A positive reading (a month of negatively skewed returns: sharp drops, small gains) "
                  "moves the centre up.",
            trades="After a month whose returns skew towards sharp drops it leans up, towards sharp spikes down."),
]
