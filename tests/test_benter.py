"""The Bill Benter bot: pooling, the journal, the fit, and the trading rules it carries."""
import json
import math

import numpy as np
import pytest
from zoo_contract import NOW, ctx_for, synthetic_bars

from glimpse_tui import bots
from glimpse_tui.zoo import benter as BT


def test_a_flat_market_says_nothing_and_a_formed_one_is_trusted():
    flat = np.full(500, 1 / 500)
    assert BT.formed(flat) == pytest.approx(0, abs=1e-9)
    x = np.arange(500)
    bell = np.exp(-0.5 * ((x - 250) / 5) ** 2)
    bell /= bell.sum()
    assert BT.formed(bell) > 0.4
    f = np.exp(-0.5 * ((x - 240) / 20) ** 2)
    f /= f.sum()
    z = BT.pool(np.log(f), np.log(flat), BT.formed(flat), 1.0, 0.5)
    c = np.exp(z - z.max())
    assert np.allclose(c / c.sum(), f)                                     # an untouched ladder leaves the fundamental alone


def test_smoothing_spreads_a_spike_and_keeps_the_mass():
    pi = np.full(500, 1e-6)
    pi[250] = 1
    pi /= pi.sum()
    s = BT.smooth(pi)
    assert s.sum() == pytest.approx(1) and s[250] < 0.5 and s[249] > 0.1 and s[251] > 0.1


def test_the_fit_recovers_the_weights_it_was_drawn_with():
    rng = np.random.default_rng(7)
    k, beta = 1.4, 0.7
    rows = []
    for _ in range(600):
        lf = np.log(rng.dirichlet(np.full(40, 2.0)) + 1e-12)
        lp = np.log(rng.dirichlet(np.full(40, 2.0)) + 1e-12)
        w = rng.uniform(0.2, 1.0)
        z = BT.pool(lf, lp, w, k, beta)
        p = np.exp(z - z.max())
        rows.append((lf, lp, w, int(rng.choice(40, p=p / p.sum()))))
    fk, fb = BT.fit(rows)
    assert fk == pytest.approx(k, abs=0.25) and fb == pytest.approx(beta, abs=0.2)


def test_the_journal_keeps_one_snapshot_a_band_and_settles_on_the_candles(tmp_path):
    bars = synthetic_bars()
    j = BT.Journal(tmp_path)
    ctx = ctx_for(bars, hours=3.0)
    ctx.end = float(bars.index[-1].timestamp() + 3600)                   # a close the candles have already settled
    ctx.now = ctx.end - 3 * 3600
    f = np.full(500, 1 / 500)
    for _ in range(3):
        j.record(ctx, f, f, 0.1)
    lines = (tmp_path / "journal.jsonl").read_text().splitlines()
    assert len(lines) == 1                                                  # once per close per band
    row = json.loads(lines[0])
    assert row["step"] == 200.0 and row["n"] == 500
    settled = j._settled(ctx)
    price = float(bars["close"].iloc[-1])
    assert len(settled) == 1 and settled[0][3] == row["idx"].index(int((price - 28_000) // 200))


def test_the_bot_trades_its_own_rules_whatever_the_default():
    b = next(x for x in bots.discover() if x.id == "benter")
    assert b.reads_market and b.family == "Benter"
    cfg = bots.BotConfig(objective="match").with_budget(100_000).for_bot(b)
    assert cfg.objective == "edge" and cfg.kelly == 0.15 and cfg.fill_margin == 0.05 and cfg.edge_close_share == 0.015


def test_its_picture_reads_the_market_and_every_buy_is_at_a_discount():
    from glimpse_tui import pricing as P
    from glimpse_tui.api import Book

    b = next(x for x in bots.discover() if x.id == "benter")
    edges = 28_000.0 + 200.0 * np.arange(501)
    bins = [(float(edges[i]), float(edges[i + 1])) for i in range(500)]
    book = Book(1, "m", int(NOW) + 7200, "live", 0, list(range(1, 501)), bins, [40.0] * 500, P.alpha_for(500))
    book.reprice()
    ctx = ctx_for(synthetic_bars(), hours=2.0)
    probs = b.probs(book, ctx)
    assert ctx.market is not None and abs(sum(probs) - 1) < 1e-9
    cfg = bots.BotConfig().with_budget(100_000).for_bot(b)
    legs = bots.decide(book, probs, cfg, cfg.max_per_cycle_sats)
    assert legs and sum(x.cost_sats for x in legs) <= 0.015 * 100_000 + 1e-6
    net = P.PAYOUT_SATS * (1 - P.FEE)
    for x in legs:                                  # each leg, priced in the order as the server fills it, is on sale
        assert probs[x.index] * net * x.contracts >= x.cost_sats * (1 + cfg.fill_margin)
    assert math.isclose(sum(probs), 1.0, rel_tol=1e-9)
