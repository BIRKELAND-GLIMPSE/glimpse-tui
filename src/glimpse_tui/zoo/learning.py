"""Pattern and machine-learning models that need nothing but the bars (the lab's `models/zoo_learning.py`).

Every model defines a signal (one value per hourly bar, from that bar and earlier ones) with sign +1. The learned
models are walk-forward, exactly as in the lab:

- the refit origins sit on a fixed UTC grid (every `refit_hours` hours since the epoch), so a new bar never moves them;
- the model fitted at origin r is trained only on origins whose 24-hour label window has closed by then;
- it predicts every bar from r up to the next refit origin.

The lab refits monthly on up to a year of origins and wants 1,000 training rows. The terminal has about 2,000 bars
and its slowest features need 720 of them, so it refits weekly and starts at 400 rows (`REFIT_HOURS`, `MIN_TRAIN`).
The lab's sklearn pieces (a one-hidden-layer MLP classifier, Bernoulli naive Bayes) are small enough to write in numpy.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import numpy as np
import pandas as pd

from .core import Ctx, Model, cached, conviction_picture
from .signals import ema, hourly_vol, log_close, rolling_slope_t, sma, zscore

FAMILY = "Pattern & ML"
LABEL_HOURS = 24
REFIT_HOURS = 168
TRAIN_HOURS = 8760
MIN_TRAIN = 400
_NS_PER_HOUR = 3_600_000_000_000
_BOOK = "Kakushadze and Serur, 151 Trading Strategies (Palgrave Macmillan, 2018)"


# ── labels and the walk-forward schedule ────────────────────

def forward_label(bars: pd.DataFrame, horizon: int = LABEL_HOURS) -> np.ndarray:
    """Forward `horizon`-hour log return from each origin's close, in units of the origin's hourly volatility times
    sqrt(horizon). NaN where the window runs past the last bar. Only ever read for origins whose window has closed."""
    lp = log_close(bars).to_numpy(dtype=float)
    vol = hourly_vol(bars).to_numpy(dtype=float)
    n = lp.size
    y = np.full(n, np.nan)
    if n > horizon:
        y[: n - horizon] = (lp[horizon:] - lp[:-horizon]) / (vol[:-horizon] * np.sqrt(horizon))
    return y


def _ns(index: pd.Index) -> np.ndarray:
    return pd.DatetimeIndex(index).as_unit("ns").asi8


def label_end_ns(ts: np.ndarray, horizon: int = LABEL_HOURS) -> np.ndarray:
    """Open time (ns) of the bar that closes each origin's label window; int64 max where it is not in the frame."""
    end = np.full(ts.size, np.iinfo(np.int64).max, dtype=np.int64)
    if ts.size > horizon:
        end[: ts.size - horizon] = ts[horizon:]
    return end


def training_rows(ts: np.ndarray, usable: np.ndarray, refit_pos: int, *, horizon: int = LABEL_HOURS,
                  train_hours: int = TRAIN_HOURS, refit_hours: int = REFIT_HOURS) -> np.ndarray:
    """Origins a model fitted at `refit_pos` may train on: features and label finite (`usable`), the label window
    closed at or before the refit origin (bar i + horizon opens no later than bar r), and the origin inside the last
    `train_hours` hours. The start is rounded up to the refit grid so it moves once per refit period, not per bar."""
    usable = np.asarray(usable, dtype=bool)
    if not usable.any():
        return np.zeros(0, dtype=np.int64)
    t_r = int(ts[refit_pos])
    first = int(ts[int(np.argmax(usable))])
    step = refit_hours * _NS_PER_HOUR
    start = int(-(-max(first, t_r - train_hours * _NS_PER_HOUR) // step) * step)
    return np.nonzero(usable & (label_end_ns(ts, horizon) <= t_r) & (ts >= start))[0]


def walk_forward(index: pd.Index, X: np.ndarray, y: np.ndarray, required: np.ndarray, *, fit: Callable[[np.ndarray, np.ndarray], Any],
                 predict: Callable[[Any, np.ndarray], np.ndarray], refit_hours: int = REFIT_HOURS, train_hours: int = TRAIN_HOURS,
                 min_train: int = MIN_TRAIN, horizon: int = LABEL_HOURS) -> tuple[np.ndarray, Any]:
    """Out-of-sample predictions per bar and the last fitted model. `required[t]` says the features every fit needs
    are finite at bar t."""
    ts = _ns(index)
    n = ts.size
    required = np.asarray(required, dtype=bool)
    usable = required & np.isfinite(y)
    out = np.full(n, np.nan)
    refits = np.nonzero(ts // _NS_PER_HOUR % refit_hours == 0)[0]
    bounds = [*refits.tolist(), n]
    last: Any = None
    for j, r in enumerate(refits):
        rows = training_rows(ts, usable, int(r), horizon=horizon, train_hours=train_hours, refit_hours=refit_hours)
        if rows.size < min_train:
            continue
        last = fit(X[rows], y[rows])
        seg = np.arange(int(r), bounds[j + 1])
        seg = seg[required[seg]]
        if seg.size:
            out[seg] = predict(last, X[seg])
    return out, last


# ── indicators (each value at bar t uses bars up to t) ──────

def _momentum(lp: pd.Series, vol: pd.Series, hours: int) -> pd.Series:
    return (lp - lp.shift(hours)) / (vol * np.sqrt(hours))


def _rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, min_periods=n).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def _macd_hist(lp: pd.Series, vol: pd.Series) -> pd.Series:
    macd = ema(lp, 12) - ema(lp, 26)
    return (macd - macd.ewm(span=9, min_periods=9).mean()) / (vol * np.sqrt(26))


def _donchian(bars: pd.DataFrame, n: int = 48) -> pd.Series:
    hi = bars["high"].astype(float).rolling(n, min_periods=n).max()
    lo = bars["low"].astype(float).rolling(n, min_periods=n).min()
    half = ((hi - lo) / 2).clip(lower=1e-9)
    return ((bars["close"].astype(float) - (hi + lo) / 2) / half).clip(-1, 1)


def _percent_b(lp: pd.Series, n: int = 20) -> pd.Series:
    m = sma(lp, n)
    sd = lp.rolling(n, min_periods=n).std().clip(lower=1e-9)
    return (lp - (m - 2 * sd)) / (4 * sd) - 0.5


def technical_features(bars: pd.DataFrame) -> pd.DataFrame:
    """Twelve technical indicators for the neural network, all scale-free."""
    lp, vol = log_close(bars), hourly_vol(bars)
    close = bars["close"].astype(float)
    feats = {f"mom_{h}h": _momentum(lp, vol, h) for h in (6, 24, 72, 168, 720)}
    feats["rsi_14"] = (_rsi(close) - 50.0) / 50.0
    feats["bollinger_pct_b"] = _percent_b(lp)
    feats["macd_hist"] = _macd_hist(lp, vol)
    feats["ema_gap_20_50"] = (ema(lp, 20) - ema(lp, 50)) / (vol * np.sqrt(50))
    feats["donchian_48h"] = _donchian(bars)
    feats["vol_ratio_24_168"] = np.log(hourly_vol(bars, span=24) / vol)
    feats["volume_z_30d"] = zscore(np.log1p(bars["volume"].astype(float).clip(lower=0)), 720)
    return pd.DataFrame(feats, index=bars.index)


def alpha_signals(bars: pd.DataFrame) -> pd.DataFrame:
    """Twelve classic single-signal strategies, each in its own scale-free units. The lab adds a thirteenth, the
    funding rate, only when its store has one; the terminal never does."""
    lp, vol = log_close(bars), hourly_vol(bars)
    close = bars["close"].astype(float)
    volume = bars["volume"].astype(float).clip(lower=0)
    sig: dict[str, pd.Series] = {}
    sig["tsmom"] = pd.concat([_momentum(lp, vol, h) for h in (24, 72, 168, 720)], axis=1).mean(axis=1, skipna=False)
    sig["momentum_skip_day"] = (lp.shift(24) - lp.shift(720)) / (vol * np.sqrt(696))
    sig["ema_crossover"] = (ema(lp, 20) - ema(lp, 50)) / (vol * np.sqrt(50))
    sig["macd_hist"] = _macd_hist(lp, vol)
    sig["donchian_48h"] = _donchian(bars) ** 3
    slope, r2 = rolling_slope_t(lp, 168)
    sig["trend_r_squared"] = slope * np.sqrt(168) / vol * r2
    sig["rsi_contrarian"] = (50.0 - _rsi(close)) / 50.0
    sig["bollinger_contrarian"] = -_percent_b(lp)
    sig["reversal_24h"] = -_momentum(lp, vol, 24)
    pv = (close * volume).rolling(168, min_periods=168).sum()
    vv = volume.rolling(168, min_periods=168).sum()
    vwap = (pv / vv.where(vv > 0)).fillna(sma(close, 168))
    sig["vwap_contrarian"] = -np.log(close / vwap) / (vol * np.sqrt(168))
    sig["volume_z"] = zscore(np.log1p(volume), 720)
    sig["vol_ratio"] = np.log(hourly_vol(bars, span=24) / vol)
    return pd.DataFrame(sig, index=bars.index)


def _standardizer(X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    mu = X.mean(axis=0)
    sd = X.std(axis=0)
    return mu, np.where(sd > 1e-12, sd, 1.0)


# ── analog nearest neighbours ───────────────────────────────

KNN_LAGS = (1, 2, 4, 8, 24, 48, 96, 168)


def knn_features(bars: pd.DataFrame) -> np.ndarray:
    lp, vol = log_close(bars), hourly_vol(bars)
    return np.column_stack([_momentum(lp, vol, h).to_numpy(dtype=float) for h in KNN_LAGS])


def neighbour_mean(cand_x: np.ndarray, cand_y: np.ndarray, x_now: np.ndarray, k: int) -> float:
    mu, sd = _standardizer(cand_x)
    d2 = np.sum(((cand_x - mu) / sd - (x_now - mu) / sd) ** 2, axis=1)
    k = min(k, d2.size)
    return float(np.mean(cand_y[np.argpartition(d2, k - 1)[:k]]))


def analog_knn_signal(bars: pd.DataFrame, k: int = 50, search_hours: int = 6000, step_hours: int = 6, min_candidates: int = 1000) -> pd.Series:
    """At each origin on a `step_hours` UTC grid: the feature vector is the volatility-scaled log return over 1, 2, 4,
    8, 24, 48, 96 and 168 hours. Candidates are past origins in the last `search_hours` whose 24-hour label window has
    closed. Features are standardized with the candidates' mean and standard deviation, the `k` nearest by Euclidean
    distance are found, and the signal is the mean of their volatility-scaled forward 24-hour returns, forward-filled
    to the bars between origins."""
    ts = _ns(bars.index)
    X, y = knn_features(bars), forward_label(bars)
    ok_x = np.all(np.isfinite(X), axis=1)
    usable = ok_x & np.isfinite(y)
    end = label_end_ns(ts)
    out = np.full(ts.size, np.nan)
    for i in np.nonzero((ts // _NS_PER_HOUR % step_hours == 0) & ok_x)[0]:
        hi = int(np.searchsorted(end, ts[i], side="right"))                  # label windows closed by bar i
        lo = int(np.searchsorted(ts, ts[i] - search_hours * _NS_PER_HOUR, side="left"))
        if hi - lo < min_candidates:
            continue
        cand = np.arange(lo, hi)
        cand = cand[usable[cand]]
        if cand.size >= min_candidates:
            out[i] = neighbour_mean(X[cand], y[cand], X[i], k)
    return pd.Series(out, index=bars.index).ffill(limit=step_hours - 1)


def analog_knn(ctx: Ctx) -> np.ndarray:
    return conviction_picture(ctx, cached(ctx, "analog_knn:signal", lambda: analog_knn_signal(ctx.bars)), sign=1, family=FAMILY)


# ── neural network (replaces sklearn's MLPClassifier) ───────

def _sigmoid(a: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(a, -50, 50)))


def mlp_fit(X: np.ndarray, labels: np.ndarray, hidden_units: int = 16, l2: float = 1.0, batch_size: int = 512, max_iter: int = 300,
            seed: int = 0, lr: float = 1e-3, tol: float = 1e-4, patience: int = 10) -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
    """One hidden layer of ReLU units and a logistic output, trained as sklearn's MLPClassifier trains by default:
    Glorot-uniform start, adam (0.001, 0.9, 0.999), shuffled minibatches, log loss plus l2 / (2 * batch) * |W|^2 on
    the weights, stopping when the epoch loss has not improved by `tol` for `patience` epochs."""
    rng = np.random.default_rng(seed)
    n, d = X.shape
    t = labels.astype(float)
    b1_, b2_ = np.sqrt(6.0 / (d + hidden_units)), np.sqrt(6.0 / (hidden_units + 1))
    params = [rng.uniform(-b1_, b1_, (d, hidden_units)), rng.uniform(-b1_, b1_, hidden_units),
              rng.uniform(-b2_, b2_, hidden_units), rng.uniform(-b2_, b2_, 1)]
    m = [np.zeros_like(p) for p in params]
    v = [np.zeros_like(p) for p in params]
    step, best, stale = 0, np.inf, 0
    bs = min(batch_size, n)
    for _ in range(max_iter):
        order = rng.permutation(n)
        epoch_loss = 0.0
        for s in range(0, n, bs):
            rows = order[s : s + bs]
            xb, tb = X[rows], t[rows]
            w1, c1, w2, c2 = params
            hid = np.maximum(xb @ w1 + c1, 0.0)
            p = _sigmoid(hid @ w2 + c2[0])
            loss = -np.mean(tb * np.log(np.clip(p, 1e-10, 1)) + (1 - tb) * np.log(np.clip(1 - p, 1e-10, 1)))
            epoch_loss += (loss + 0.5 * l2 * (np.sum(w1 * w1) + np.dot(w2, w2)) / rows.size) * rows.size
            delta = (p - tb) / rows.size
            dh = np.outer(delta, w2) * (hid > 0)
            grads = [xb.T @ dh + l2 * w1 / rows.size, dh.sum(axis=0), hid.T @ delta + l2 * w2 / rows.size, np.array([delta.sum()])]
            step += 1
            for q, g in enumerate(grads):
                m[q] = 0.9 * m[q] + 0.1 * g
                v[q] = 0.999 * v[q] + 0.001 * g * g
                params[q] = params[q] - lr * np.sqrt(1 - 0.999**step) / (1 - 0.9**step) * m[q] / (np.sqrt(v[q]) + 1e-8)
        epoch_loss /= n
        stale = stale + 1 if epoch_loss > best - tol else 0
        best = min(best, epoch_loss)
        if stale > patience:
            break
    return params[0], params[1], params[2], float(params[3][0])


def mlp_proba(net: tuple[np.ndarray, np.ndarray, np.ndarray, float], X: np.ndarray) -> np.ndarray:
    w1, c1, w2, c2 = net
    return _sigmoid(np.maximum(X @ w1 + c1, 0.0) @ w2 + c2)


def fit_network(X: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray, Any]:
    mu, sd = _standardizer(X)
    labels = (y > 0).astype(int)
    if np.unique(labels).size < 2:
        return mu, sd, float(labels.mean())
    return mu, sd, mlp_fit(np.clip((X - mu) / sd, -5, 5), labels)


def predict_up(model: tuple[np.ndarray, np.ndarray, Any], X: np.ndarray) -> np.ndarray:
    mu, sd, net = model
    if isinstance(net, float):
        return np.full(X.shape[0], net - 0.5)
    return mlp_proba(net, np.clip((X - mu) / sd, -5, 5)) - 0.5


def ann_direction_signal(bars: pd.DataFrame) -> pd.Series:
    X = technical_features(bars).to_numpy(dtype=float)
    out, _ = walk_forward(bars.index, X, forward_label(bars), np.all(np.isfinite(X), axis=1), fit=fit_network, predict=predict_up)
    return pd.Series(out, index=bars.index)


def ann_direction(ctx: Ctx) -> np.ndarray:
    return conviction_picture(ctx, cached(ctx, "ann_direction:signal", lambda: ann_direction_signal(ctx.bars)), sign=1, family=FAMILY)


# ── alpha combination ───────────────────────────────────────

def fit_ridge(X: np.ndarray, y: np.ndarray, ridge_alpha: float = 10.0) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Closed-form ridge on standardized signals; the intercept is left unpenalized by centring the label."""
    mu, sd = _standardizer(X)
    z = (X - mu) / sd
    return mu, sd, np.linalg.solve(z.T @ z + ridge_alpha * np.eye(z.shape[1]), z.T @ (y - y.mean()))


def predict_combo(model: tuple[np.ndarray, np.ndarray, np.ndarray], X: np.ndarray) -> np.ndarray:
    mu, sd, w = model
    return ((X - mu) / sd) @ w


def alpha_combo_signal(bars: pd.DataFrame) -> pd.Series:
    X = alpha_signals(bars).to_numpy(dtype=float)
    out, _ = walk_forward(bars.index, X, forward_label(bars), np.all(np.isfinite(X), axis=1), fit=fit_ridge, predict=predict_combo)
    return pd.Series(out, index=bars.index)


def alpha_combo(ctx: Ctx) -> np.ndarray:
    return conviction_picture(ctx, cached(ctx, "alpha_combo:signal", lambda: alpha_combo_signal(ctx.bars)), sign=1, family=FAMILY)


# ── candlestick patterns ────────────────────────────────────

def candlestick_counts(bars: pd.DataFrame, trend_hours: int = 12) -> pd.DataFrame:
    """Bullish and bearish pattern flags per bar. Per hourly candle: body = |close - open|, range = high - low, upper
    shadow = high - max(open, close), lower shadow = min(open, close) - low. The prior trend is the log return over
    the `trend_hours` bars before the candle in units of volatility: below -0.5 is a downtrend, above +0.5 an uptrend.
    Patterns (Nison 1991):

    - bullish engulfing: downtrend, previous candle bearish with body > 0.1 of its range, current candle bullish,
      opens at or below the previous close, closes at or above the previous open, and has the larger body;
    - bearish engulfing: the mirror image after an uptrend;
    - hammer: downtrend, body between 0.1 and 1/3 of the range, lower shadow >= 2 bodies, upper shadow <= 0.1 range;
    - shooting star: uptrend, body between 0.1 and 1/3 of the range, upper shadow >= 2 bodies, lower shadow <= 0.1 range;
    - doji after a trend: body <= 0.1 of the range; bullish after a downtrend, bearish after an uptrend."""
    o, h, lo, c = (bars[k].astype(float) for k in ("open", "high", "low", "close"))
    rng = (h - lo).clip(lower=1e-12)
    body = (c - o).abs()
    upper = h - np.maximum(o, c)
    lower = np.minimum(o, c) - lo
    lp, vol = log_close(bars), hourly_vol(bars)
    trend = (lp.shift(1) - lp.shift(1 + trend_hours)) / (vol.shift(1) * np.sqrt(trend_hours))
    down, up = trend < -0.5, trend > 0.5
    real = h > lo
    po, pc, pbody, prng = o.shift(1), c.shift(1), body.shift(1), rng.shift(1)
    not_doji_prev = pbody > 0.1 * prng
    bull_engulf = down & (pc < po) & (c > o) & (o <= pc) & (c >= po) & (body > pbody) & not_doji_prev
    bear_engulf = up & (pc > po) & (c < o) & (o >= pc) & (c <= po) & (body > pbody) & not_doji_prev
    small_body = (body > 0.1 * rng) & (body <= rng / 3)
    hammer = down & real & small_body & (lower >= 2 * body) & (upper <= 0.1 * rng)
    shooting_star = up & real & small_body & (upper >= 2 * body) & (lower <= 0.1 * rng)
    doji = real & (body <= 0.1 * rng)
    bull = bull_engulf.astype(float) + hammer.astype(float) + (doji & down).astype(float)
    bear = bear_engulf.astype(float) + shooting_star.astype(float) + (doji & up).astype(float)
    valid = trend.notna()
    return pd.DataFrame({"bullish": bull.where(valid), "bearish": bear.where(valid)}, index=bars.index)


def candlestick_signal(bars: pd.DataFrame, count_hours: int = 24, trend_hours: int = 12) -> pd.Series:
    """Bullish minus bearish pattern count over the last `count_hours` candles, smoothed by a 4-hour EMA."""
    flags = candlestick_counts(bars, trend_hours)
    net = (flags["bullish"] - flags["bearish"]).rolling(count_hours, min_periods=count_hours).sum()
    return net.ewm(span=4, min_periods=1).mean().where(net.notna())


def candlestick_patterns(ctx: Ctx) -> np.ndarray:
    return conviction_picture(ctx, cached(ctx, "candlestick_patterns:signal", lambda: candlestick_signal(ctx.bars)), sign=1, family=FAMILY)


# ── naive Bayes on market state (replaces sklearn's BernoulliNB) ────────────

def state_features(bars: pd.DataFrame) -> pd.DataFrame:
    """The lab's three price facts per bar: 1.0, 0.0, or NaN where an input is missing. Its five optional facts (Fear
    and Greed, funding, DVOL) need feeds the terminal does not have, and the lab drops them when they are missing."""
    close = bars["close"].astype(float)
    volume = bars["volume"].astype(float)
    r24 = close / close.shift(24)
    s168 = sma(close, 168)
    vmed = volume.rolling(720, min_periods=720).median()

    def binary(cond: pd.Series, *inputs: pd.Series) -> np.ndarray:
        ok = np.ones(len(cond), dtype=bool)
        for s in inputs:
            ok &= np.isfinite(s.to_numpy(dtype=float))
        return np.where(ok, cond.to_numpy(dtype=bool).astype(float), np.nan)

    return pd.DataFrame({"return_24h_positive": binary(r24 > 1.0, r24), "above_sma_168h": binary(close > s168, s168),
                         "volume_above_30d_median": binary(volume > vmed, vmed)}, index=bars.index)


def fit_bayes(X: np.ndarray, y: np.ndarray, nb_alpha: float = 1.0) -> Any:
    """Bernoulli naive Bayes with Laplace smoothing: per class, the log prior and the log odds of each fact."""
    labels = (y > 0).astype(int)
    if labels.size == 0 or np.unique(labels).size < 2:
        return float(labels.mean()) if labels.size else 0.5
    prior, logp, log1mp = [], [], []
    for c in (0, 1):
        rows = X[labels == c]
        p = (rows.sum(axis=0) + nb_alpha) / (rows.shape[0] + 2 * nb_alpha)
        prior.append(np.log(rows.shape[0] / labels.size))
        logp.append(np.log(p))
        log1mp.append(np.log(1 - p))
    return np.array(prior), np.array(logp), np.array(log1mp)


def predict_bayes(model: Any, X: np.ndarray) -> np.ndarray:
    if isinstance(model, float):
        return np.full(X.shape[0], model - 0.5)
    prior, logp, log1mp = model
    joint = prior[None, :] + X @ logp.T + (1 - X) @ log1mp.T
    return _sigmoid(joint[:, 1] - joint[:, 0]) - 0.5


def naive_bayes_signal(bars: pd.DataFrame) -> pd.Series:
    X = state_features(bars).to_numpy(dtype=float)
    out, _ = walk_forward(bars.index, X, forward_label(bars), np.all(np.isfinite(X), axis=1), fit=fit_bayes, predict=predict_bayes)
    return pd.Series(out, index=bars.index)


def naive_bayes_sentiment(ctx: Ctx) -> np.ndarray:
    return conviction_picture(ctx, cached(ctx, "naive_bayes_sentiment:signal", lambda: naive_bayes_signal(ctx.bars)), sign=1, family=FAMILY)


_FACTORS = ("direction", "tails")

# Shared by every model here: sign +1 into conviction_picture with the family's shape and clock.
_SHAPE = ("sign = +1 (a positive reading leans the picture up), family Pattern & ML: the drift rides at full strength at every "
          "horizon and the tail on the signal's side is fattened.")
# The walk-forward schedule the three learned models share (walk_forward, training_rows).
_WALK = ("Walk-forward: refit origins sit on a fixed weekly grid (hours since the epoch divisible by 168, so Thursday 00:00 UTC) "
         "that a new bar never moves. The fit at origin r trains on the bars of the last 8,760 hours (start rounded up to the "
         "weekly grid) whose features and label are finite and whose label window had closed by r (bar i+24 opened no later "
         "than bar r); it needs 400 such rows, otherwise that week stays unread. The fitted model then scores every bar from r "
         "to the next origin. The label is y_i = (ℓ_i+24 − ℓ_i)/(v_i·√24), the forward 24-hour log return in units of the "
         "origin's hourly volatility v (EWMA standard deviation, span 168) times √24. The whole series, refits included, is "
         "recomputed once per new bar and cached for every market on the same bars. The lab refits monthly on up to a year "
         "and wants 1,000 rows; the terminal refits weekly at 400.")
# When the walk-forward models first speak on the terminal's bars.
_WALK_INPUTS = ("The slowest feature needs 720 bars, a training row needs its 24-hour outcome, and the first fit needs 400 rows, "
                "so the first reading arrives at the first Thursday 00:00 UTC origin after about 1,150 bars (up to 1,310); the "
                "strength percentile needs 200 readings on top of that.")


def _m(model_id: str, name: str, blurb: str, description: str, fn: Callable[[Ctx], np.ndarray], reference: str, *,
       inputs: str, maths: str, trades: str) -> Model:
    return Model(model_id, name, FAMILY, blurb, description, fn, factors=_FACTORS, reference=reference,
                 inputs=inputs, maths=maths, trades=trades, pipeline="conviction")


MODELS = [
    _m("analog_knn", "Analogs", "Finds the 50 moments in history that looked most like now",
       "History rhymes: if the last hour, day and week of price moves look like some earlier moments, what followed "
       "those moments is a fair guess for what follows now. The model describes the recent path in units of "
       "volatility, finds the 50 closest matches among the loaded bars, and averages what Bitcoin did in the 24 hours "
       "after each of them. Analogues that mostly rose lean the picture bullish; ones that mostly fell lean it "
       "bearish. Only past moments whose next day had already played out are used.",
       analog_knn, f"{_BOOK}, Section 3.17: Machine learning, single-stock KNN",
       inputs="Hourly closes over the last 168 hours (7 days) for the features, the EWMA hourly volatility (span 168), and the "
              "forward 24-hour returns of past bars as outcomes. The search window is 6,000 hours (250 days), more than the "
              "terminal ever holds, so every loaded bar is a candidate. It needs 1,000 candidate bars with a closed outcome "
              "(about 1,190 bars) before it reads anything, and 200 readings on top for the strength percentile; the lab "
              "searches eight months, the terminal its twelve weeks or so.",
       maths="At every origin t on the 6-hour UTC grid (00, 06, 12, 18) the feature vector is x_t = (ℓ_t − ℓ_t−n)/(v_t·√n) for "
             "n ∈ {1, 2, 4, 8, 24, 48, 96, 168}, ℓ = ln close. Candidates are every bar i (not only grid origins) within the "
             "last 6,000 hours with finite features and a closed label, bar i+24 having opened no later than bar t, with "
             "y_i = (ℓ_i+24 − ℓ_i)/(v_i·√24); fewer than 1,000 means no reading. Each feature is standardised by the candidates' "
             "mean and standard deviation, the Euclidean distance from x_t to each candidate is taken, and the signal is the "
             "mean y_i of the k = 50 nearest; between origins the value is carried forward for up to 5 bars. Nothing is fitted: "
             "each origin is its own search, and the series is recomputed once per new bar and cached across markets. "
             "Adjacent hours have near-identical features, so the 50 neighbours tend to come in short runs of consecutive bars. "
             + _SHAPE,
       trades="When the neighbours' next days mostly rose it puts more weight than the market on the ranges from about +0.5σ "
              "above spot out to the far upside and buys those; it strips weight from the ranges below spot and never buys "
              "them. When they mostly fell, the mirror image, at any horizon. A neighbour average near the middle of its own "
              "history leaves the baseline and the bot buys nothing."),
    _m("candlestick_patterns", "Candlestick Patterns", "Counts bullish and bearish reversal candles over the last day",
       "The Japanese candlestick patterns traders spot by eye, counted by machine. A reversal candle after a run, "
       "such as a hammer after a fall or a shooting star after a rise, is read as the moment the other side took "
       "control. Over the last 24 hourly candles the model counts bullish reversal patterns (a bullish engulfing "
       "candle, a hammer, or a doji after a fall) and bearish ones (a bearish engulfing candle, a shooting star, or a "
       "doji after a rise). More bullish patterns lean the picture bullish, more bearish ones lean it bearish.",
       candlestick_patterns, "Japanese candlestick charting techniques (Nison 1991)",
       inputs="Open, high, low and close of the last 24 hourly candles, the 13 closes before each candle for its prior trend, "
              "and the EWMA hourly volatility (span 168, which needs 48 bars). A reading exists from about the 72nd bar, so "
              "the 400-bar minimum already gives the 200 readings the strength percentile needs. Nothing is fitted.",
       maths="Per candle: body = |C − O|, range = H − L (floored at 10⁻¹²), upper shadow = H − max(O, C), lower shadow = "
             "min(O, C) − L; the prior trend T = (ℓ_t−1 − ℓ_t−13)/(v_t−1·√12) is a downtrend below −0.5 and an uptrend above "
             "+0.5. Bullish engulfing: downtrend, previous candle bearish with body > 0.1 of its range, this candle bullish, "
             "O ≤ previous C, C ≥ previous O and the larger body; bearish engulfing is the mirror after an uptrend. Hammer: "
             "downtrend, H > L, body between 0.1 and 1/3 of the range, lower shadow ≥ 2 bodies, upper shadow ≤ 0.1 range; "
             "shooting star the mirror after an uptrend. Doji: H > L and body ≤ 0.1 range, bullish after a downtrend, bearish after an "
             "uptrend. Bull_t and bear_t are the counts of those flags; the signal is the 24-bar rolling sum of bull − bear "
             "smoothed by an EMA of span 4 (α = 0.4). " + _SHAPE,
       trades="When the last day held more bullish than bearish reversal candles it puts more weight than the market on the "
              "ranges from about +0.5σ above spot out to the far upside and buys those, stripping weight from the ranges "
              "below spot; more bearish candles give the mirror image, at any horizon. A net count near the middle of its own "
              "history, including the frequent zero, leaves the baseline and the bot buys nothing."),
    _m("ann_direction", "Neural Net Direction", "A small neural network reads a dozen chart indicators",
       "A small neural network learns which mixes of a dozen chart indicators came before an up day. Momentum, RSI, "
       "Bollinger position, MACD, volatility and volume each say something on their own; a hidden layer lets the "
       "network learn when they say something together. It outputs the probability that Bitcoin is higher in 24 "
       "hours, is retrained weekly on hours whose outcome was already known, and is used untouched until the next "
       "retrain. A probability above one half leans the picture bullish, below leans it bearish.",
       ann_direction, f"{_BOOK}, Section 18.2: Artificial neural network (ANN)",
       inputs="Hourly close, high, low and volume over the last 720 hours (30 days) for the twelve features and the EWMA "
              "hourly volatility (span 168); forward 24-hour returns of past bars as labels. " + _WALK_INPUTS,
       maths="Features at bar t, with ℓ = ln close and v the hourly volatility: momentum (ℓ_t − ℓ_t−n)/(v·√n) for n ∈ {6, 24, "
             "72, 168, 720}; (RSI₁₄ − 50)/50 with Wilder's RSI (EWM α = 1/14 of up and down moves); Bollinger %B − 0.5 on ℓ "
             "over 20 bars (2 standard deviations); MACD histogram (EMA₁₂ − EMA₂₆ of ℓ less its EMA₉)/(v·√26); "
             "(EMA₂₀ − EMA₅₀)/(v·√50); the close's position in the 48-bar high-low channel, in [−1, 1]; ln(v₂₄/v₁₆₈) with "
             "v₂₄ the span-24 EWMA volatility; and the 720-bar z-score of ln(1 + volume). Each fit standardises the features "
             "by the training mean and standard deviation, clips them to ±5 and labels a row 1 when y > 0. The network is "
             "12 → 16 ReLU units → 1 logistic output, Glorot-uniform start from seed 0, adam (learning rate 0.001, β 0.9 and "
             "0.999, ε 10⁻⁸), shuffled minibatches of 512, loss = mean log loss + 1.0·(|W₁|² + |W₂|²)/(2·batch) on the weights "
             "only, at most 300 epochs, stopping once the epoch loss has failed to improve by 10⁻⁴ for more than 10 epochs; "
             "the lab fits the same network with scikit-learn's MLPClassifier. If a training set has one class, P(up) is a "
             "constant, the share of up rows. The signal is P(up) − 0.5. " + _WALK + " " + _SHAPE,
       trades="When the network's probability of an up day is high it puts more weight than the market on the ranges from "
              "about +0.5σ above spot out to the far upside and buys those, stripping weight from the ranges below spot and "
              "never buying them; a low probability gives the mirror image, at any horizon. A probability near the middle of "
              "its own history, or a week whose fit lacked 400 rows, leaves the baseline and the bot buys nothing."),
    _m("alpha_combo", "Alpha Combo", "Twelve classic strategies voting as one",
       "Twelve classic strategies voting as one: trend-following momentum, moving-average and MACD crossovers, channel "
       "breakouts, a steady-trend score, RSI and Bollinger contrarians, 24-hour reversal, VWAP pull, volume and "
       "volatility. No single one is reliable, but a regression on past outcomes can learn how much weight each has "
       "earned in predicting the next day's return, and shrink the weights of the noisy ones. The weighted sum is the "
       "signal: pointing up leans the picture bullish, pointing down bearish. The lab adds perpetual funding as a "
       "thirteenth voice when it has the data; the terminal does not have it.",
       alpha_combo, f"{_BOOK}, Section 3.20: Alpha combos (Kakushadze and Yu); ridge regression (Hoerl and Kennard 1970)",
       inputs="Hourly close, high, low and volume over the last 720 hours (30 days) for the twelve signals and the EWMA hourly "
              "volatility (span 168); forward 24-hour returns of past bars as labels. " + _WALK_INPUTS,
       maths="Signals at bar t, with ℓ = ln close and v the hourly volatility: tsmom, the mean of (ℓ_t − ℓ_t−n)/(v·√n) over n ∈ "
             "{24, 72, 168, 720}; skip-a-day momentum (ℓ_t−24 − ℓ_t−720)/(v·√696); (EMA₂₀ − EMA₅₀)/(v·√50); the MACD histogram "
             "(EMA₁₂ − EMA₂₆ less its EMA₉)/(v·√26); the cube of the close's position in the 48-bar high-low channel; the "
             "168-bar OLS slope of ℓ on time ×√168/v × R²; (50 − RSI₁₄)/50; −(Bollinger %B − 0.5) over 20 bars; "
             "−(ℓ_t − ℓ_t−24)/(v·√24); −ln(close/VWAP₁₆₈)/(v·√168) with VWAP the volume-weighted mean close over 168 bars "
             "(the plain 168-bar mean where volume sums to zero); the 720-bar z-score of ln(1 + volume); and ln(v₂₄/v₁₆₈). "
             "Each fit standardises the signals by the training mean and standard deviation into Z and solves the ridge "
             "w = (ZᵀZ + 10·I)⁻¹ Zᵀ(y − ȳ), λ = 10 with the label centred so the intercept is unpenalised. The signal is "
             "z_t·w: the predicted forward 24-hour vol-scaled return relative to the training-period mean, no intercept added. "
             + _WALK + " " + _SHAPE,
       trades="When the weighted vote points up it puts more weight than the market on the ranges from about +0.5σ above spot "
              "out to the far upside and buys those, stripping weight from the ranges below spot and never buying them; a "
              "vote pointing down gives the mirror image, at any horizon. A prediction near the middle of its own history, or "
              "a week whose fit lacked 400 rows, leaves the baseline and the bot buys nothing."),
    _m("naive_bayes_sentiment", "Naive Bayes State", "Three yes-or-no price facts, turned into the odds of an up day",
       "Reads the state of the market as three yes-or-no facts: is the price up on the day, above its weekly average, "
       "and trading on above-normal volume? Each fact has been more or less common before up days than before down "
       "days, and treating them as independent lets those frequencies multiply into the odds of an up day. Above one "
       "half leans bullish, below leans bearish. In the lab the same model also reads the Fear and Greed index, "
       "perpetual funding and implied volatility (DVOL); the terminal has none of those feeds, so only the three price "
       "facts remain and there is no sentiment in it.",
       naive_bayes_sentiment, f"{_BOOK}, Section 18.3: Sentiment analysis, naive Bayes Bernoulli",
       inputs="Hourly closes over the last 168 hours (7 days) and volume over the last 720 hours (30 days) for the three facts; "
              "forward 24-hour returns of past bars as labels. " + _WALK_INPUTS,
       maths="Facts at bar t, each 1 or 0 (missing while an input is unavailable): close_t > close_t−24; close_t > the 168-bar "
             "mean close; volume_t > the 720-bar rolling median volume. Each fit is Bernoulli naive Bayes with Laplace "
             "smoothing α = 1 on the label up = (y > 0): per class c the prior is n_c/n and the probability of fact j is "
             "p_cj = (n_cj + 1)/(n_c + 2). For a bar with facts x the log score of class c is ln prior_c + Σ_j [x_j·ln p_cj + "
             "(1 − x_j)·ln(1 − p_cj)], and the signal is σ(score_up − score_down) − 0.5, that is P(up | x) − 0.5, with σ the "
             "logistic function. A one-class training set gives a constant P(up), the share of up rows. This replaces the lab's scikit-learn "
             "BernoulliNB. With three binary facts the signal takes at most eight values per fit, so its percentile moves "
             "in steps. " + _WALK + " " + _SHAPE,
       trades="When the three facts have preceded up days more often than not it puts more weight than the market on the "
              "ranges from about +0.5σ above spot out to the far upside and buys those, stripping weight from the ranges below "
              "spot and never buying them; facts that have preceded down days give the mirror image, at any horizon. A "
              "probability near the middle of its own history, or a week whose fit lacked 400 rows, leaves the baseline and "
              "the bot buys nothing."),
]
