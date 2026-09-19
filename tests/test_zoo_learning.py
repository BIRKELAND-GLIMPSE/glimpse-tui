import numpy as np
import pandas as pd
from zoo_contract import NOW, check_model, ctx_for, synthetic_bars

from glimpse_tui.zoo import core as C
from glimpse_tui.zoo import learning as L


def test_every_model_meets_the_contract():
    assert len({m.id for m in L.MODELS}) == len(L.MODELS)
    for m in L.MODELS:
        check_model(m)


def test_every_signal_is_alive_on_two_thousand_bars():
    bars = synthetic_bars(2000)
    for build in (L.analog_knn_signal, L.candlestick_signal, L.ann_direction_signal, L.alpha_combo_signal, L.naive_bayes_signal):
        s = build(bars)
        assert s.notna().sum() >= C.MIN_HISTORY and np.isfinite(s.iloc[-1]) and s.nunique() > 3, build.__name__


def test_signals_are_causal():
    bars = synthetic_bars(2000)
    cut = 1900
    for build in (L.analog_knn_signal, L.candlestick_signal, L.ann_direction_signal, L.alpha_combo_signal, L.naive_bayes_signal):
        full, early = build(bars).iloc[:cut], build(bars.iloc[:cut])
        assert np.allclose(full.to_numpy(), early.to_numpy(), equal_nan=True), build.__name__


def test_training_rows_never_see_an_open_label_window():
    bars = synthetic_bars(1200)
    ts = L._ns(bars.index)
    r = int(np.nonzero(ts // L._NS_PER_HOUR % L.REFIT_HOURS == 0)[0][-1])
    rows = L.training_rows(ts, np.ones(ts.size, dtype=bool), r)
    assert rows.size and rows.max() + L.LABEL_HOURS <= r


def candles(rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    """A falling (or rising) run of plain candles is prepended by the caller; this just frames OHLC rows."""
    idx = pd.date_range(end=pd.Timestamp((int(NOW) // 3600 - 1) * 3600, unit="s", tz="UTC"), periods=len(rows), freq="h")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx).assign(volume=100.0)


def trend_then(last: list[tuple[float, float, float, float]], step: float) -> pd.DataFrame:
    rng = np.random.default_rng(0)
    rows, px = [], 100.0
    for _ in range(120):                                       # noise so the volatility estimate is sane
        nxt = px * (1 + rng.normal(0, 0.002))
        rows.append((px, max(px, nxt) * 1.0005, min(px, nxt) * 0.9995, nxt))
        px = nxt
    for _ in range(14):                                        # the prior trend
        nxt = px * (1 + step)
        rows.append((px, max(px, nxt) * 1.0005, min(px, nxt) * 0.9995, nxt))
        px = nxt
    return candles(rows + [tuple(px * v for v in c) for c in last])


def test_candlestick_flags_have_the_right_sign():
    hammer = trend_then([(1.0, 1.0012, 0.99, 1.001)], -0.004)           # long lower shadow after a fall
    star = trend_then([(1.0, 1.01, 0.9988, 0.999)], +0.004)             # long upper shadow after a rise
    engulf = trend_then([(1.0, 1.0005, 0.9955, 0.996), (0.9955, 1.0025, 0.995, 1.002)], -0.004)
    assert L.candlestick_counts(hammer).iloc[-1].tolist() == [1.0, 0.0]
    assert L.candlestick_counts(star).iloc[-1].tolist() == [0.0, 1.0]
    assert L.candlestick_counts(engulf).iloc[-1].tolist() == [1.0, 0.0]


def test_candlestick_picture_leans_with_the_patterns():
    bars = synthetic_bars(1500)
    sig = L.candlestick_signal(bars)
    hi, lo = int(np.nanargmax(sig.to_numpy())), int(np.nanargmin(sig.to_numpy()))
    for pos, sign in ((hi, 1), (lo, -1)):
        cut = bars.iloc[: pos + 1]
        if len(cut) < 800:
            continue
        shifted = cut.set_axis(bars.index[-len(cut):])              # same candles, ending at the contract's clock
        ctx = ctx_for(shifted, 24.0)
        assert sign * C.describe(ctx, L.candlestick_patterns(ctx))["lean"] > 0.2


def test_neighbour_mean_reads_the_closest_analogues():
    rng = np.random.default_rng(1)
    x = rng.standard_normal((2000, 8))
    y = np.sign(x[:, 0]) + 0.1 * rng.standard_normal(2000)
    probe = np.zeros(8)
    probe[0] = 2.0
    assert L.neighbour_mean(x, y, probe, 50) > 0.8 and L.neighbour_mean(x, y, -probe, 50) < -0.8


def test_the_network_learns_a_nonlinear_rule():
    rng = np.random.default_rng(2)
    x = rng.standard_normal((1500, 12))
    y = np.where(x[:, 0] * x[:, 1] > 0, 1.0, -1.0)                      # XOR: no linear model can learn this
    model = L.fit_network(x[:1200], y[:1200])
    hit = np.mean(np.sign(L.predict_up(model, x[1200:])) == y[1200:])
    assert hit > 0.8


def test_ridge_and_bayes_point_the_right_way():
    rng = np.random.default_rng(3)
    x = rng.standard_normal((1000, 12))
    y = 0.5 * x[:, 3] + rng.standard_normal(1000)
    w = L.fit_ridge(x, y)[2]
    assert int(np.argmax(np.abs(w))) == 3 and w[3] > 0.3
    facts = (rng.random((1000, 3)) < 0.5).astype(float)
    up = np.where(rng.random(1000) < 0.2 + 0.6 * facts[:, 0], 1.0, -1.0)  # the first fact makes an up day likely
    pred = L.predict_bayes(L.fit_bayes(facts, up), np.array([[1.0, 0.0, 0.0], [0.0, 0.0, 0.0]]))
    assert pred[0] > 0.2 and pred[1] < -0.2
