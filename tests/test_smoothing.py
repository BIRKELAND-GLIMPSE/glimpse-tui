"""The smoothing bots: the series read as one surface, each close's curve carried to the focused horizon, five ways of
combining them with the fundamental, the band premise, and the runner handing a series bot every open close."""
import numpy as np
import pytest
from test_bots import FakeApi, runner
from zoo_contract import NOW, check_model, ctx_for, synthetic_bars

from glimpse_tui import bots
from glimpse_tui import policy as PL
from glimpse_tui import pricing as P
from glimpse_tui.zoo import benter, smoothing
from glimpse_tui.zoo import core as C

N = 500


def bell(centre: float, width: float) -> np.ndarray:
    p = np.exp(-0.5 * ((np.arange(N) - centre) / width) ** 2) + 1e-9
    return p / p.sum()


def consistent_series(ctx: C.Ctx, closes: int = 36):
    """A surface every close of which is the random walk's own picture at its horizon: a bell as wide as the
    volatility clock says, centred on spot, for the hourly closes after NOW."""
    centre = (ctx.spot - ctx.edges[0]) / 200.0
    first = (int(NOW) // 3600 + 1) * 3600
    out = []
    for j in range(closes):
        end = float(first + j * 3600)
        width = smoothing.sigma_to(ctx, end) * ctx.spot / 200.0
        out.append(C.Shown(end, ctx.edges, bell(centre, width)))
    return tuple(out), centre


def jagged(curve: np.ndarray, centre: int) -> np.ndarray:
    """The same curve after a few tickets: one range bid up far above its neighbours, another left thin."""
    q = curve.copy()
    q[centre + 6] *= 4.0                           # the spike
    q[centre - 3] *= 0.25                          # the dip
    return q / q.sum()


def roughness(p: np.ndarray) -> float:
    """How jagged a curve is as the 3D view draws it, height for probability: the sum of its second differences."""
    return float(np.abs(np.diff(p, 2)).sum())


@pytest.mark.parametrize("m", smoothing.MODELS, ids=lambda m: m.id)
def test_every_smoothing_bot_is_a_zoo_model_that_reads_the_series_and_carries_its_own_rules(m):
    check_model(m)                                                          # no series in sight: still a sound picture
    assert m.family == "Smoothing" and m.reads_market and m.reads_series and m.policy is None
    assert m.trading == smoothing.TRADING and m.trading["objective"] == "edge" and m.trading["interval_s"] == 3
    sections = dict(C.explain(m))
    assert "carried to the focused close's horizon" in sections["machinery"] and "Kelly" in sections["trades"]


def test_without_a_series_or_a_market_every_bot_draws_the_fundamental():
    ctx = ctx_for(synthetic_bars(), 5.0)
    f = benter.fundamental(ctx)
    for m in smoothing.MODELS:
        assert np.allclose(m.fn(ctx), f), m.id


def test_carrying_a_curve_keeps_its_picture_in_standardised_units():
    ctx = ctx_for(synthetic_bars(), 5.0)
    z = ctx.edge_z
    curve = bell(262, 5.0)
    carried = smoothing.carry(curve, ctx.sigma / 2, ctx.sigma, z)          # from a close with half this one's σ
    assert carried.sum() == pytest.approx(1) and np.all(carried > 0)
    own = smoothing.quantiles(curve, z * 2, [0.1, 0.5, 0.9])                # the source in its own z: the same prices, twice as far out
    assert smoothing.quantiles(carried, z, [0.1, 0.5, 0.9]) == pytest.approx(own, abs=0.02)
    # in price the curve is twice as wide after the carry: a shorter horizon's picture spread over a longer one
    m0, s0 = smoothing.moments(curve, np.arange(N + 1.0))
    m1, s1 = smoothing.moments(carried, np.arange(N + 1.0))
    assert s1 == pytest.approx(2 * s0, rel=0.05) and m1 == pytest.approx(m0 + (m0 - 250) , abs=3)   # and leans twice as far from spot


def test_formedness_is_zero_for_a_flat_ladder_and_one_at_the_fundamental():
    f = bell(250, 8.0)
    flat = np.full(N, 1 / N)
    assert smoothing.formed(flat, f) == pytest.approx(0, abs=1e-9)
    assert smoothing.formed(f, f) == pytest.approx(1)
    assert smoothing.formed(bell(250, 3.0), f) == 1                          # sharper than f: capped
    half = 0.5 * f + 0.5 * flat
    assert 0.1 < smoothing.formed(half, f) < 0.9


def test_the_pin_puts_the_reference_band_mass_inside_the_band():
    ref, wide = bell(250, 6.0), bell(250, 9.0)
    lo, hi = smoothing.band(ref)
    target = ref[lo:hi + 1].sum()
    assert 0.8 <= target < 0.9 and wide[lo:hi + 1].sum() < 0.7
    pinned = smoothing.pin(wide, ref, 1.0)
    assert pinned[lo:hi + 1].sum() == pytest.approx(target, abs=1e-3)
    assert smoothing.pin(wide, ref, 0.0) is wide                            # no evidence: untouched
    half = smoothing.pin(wide, ref, 0.5)
    assert wide[lo:hi + 1].sum() < half[lo:hi + 1].sum() < pinned[lo:hi + 1].sum()


def surface_case(hours: float = 5.0):
    """A consistent surface, with the focused close knocked out of line by a few tickets."""
    ctx = ctx_for(synthetic_bars(), hours)
    series, centre = consistent_series(ctx)
    k = next(i for i, s in enumerate(series) if abs(s.end - ctx.end) < 1)
    smooth = series[k].probs
    market = jagged(smooth, int(round(centre)))
    series = series[:k] + (C.Shown(series[k].end, ctx.edges, market),) + series[k + 1:]
    ctx.series, ctx.market = series, market
    return ctx, smooth, market, int(round(centre))


@pytest.mark.parametrize("m", smoothing.MODELS, ids=lambda m: m.id)
def test_a_consistent_surface_pulls_a_jagged_close_back_into_line(m):
    ctx, smooth, market, c = surface_case()
    p = np.asarray(m.fn(ctx))
    assert p.shape == (N,) and p.sum() == pytest.approx(1) and np.all(p > 0)
    assert roughness(p) < 0.5 * roughness(market)                          # smoother than what the site shows
    tv = lambda a, b: 0.5 * np.abs(a - b).sum()                            # noqa: E731
    assert tv(p, smooth) < tv(market, smooth)                               # and nearer the surface's own line
    # the dip is worth more than the market says and the spike less: the picture buys the dip, not the spike
    assert p[c - 3] / market[c - 3] > 1.5 > p[c + 6] / market[c + 6]
    assert np.allclose(p, m.fn(ctx))                                        # the same inputs draw the same picture


def test_the_surface_weighs_formed_near_closes_and_pools_them_into_one_belief():
    ctx, smooth, market, c = surface_case()
    f = benter.fundamental(ctx)
    s = smoothing.Surface(ctx, f)
    assert s.focus is s.closes[0] and len(s.closes) <= 2 * smoothing.WINDOW + 1
    assert all(0 <= x.formed <= 1 and 0 < x.kernel <= 1 for x in s.closes) and s.focus.kernel == 1
    near = sorted(s.closes[1:], key=lambda x: abs(x.hours - ctx.hours))
    assert near[0].kernel > near[-1].kernel                                 # the nearest neighbour weighs most
    assert 0.9 < s.evidence <= 1                                            # a dozen formed closes: all the evidence it needs
    belief = s.belief()
    assert roughness(belief) < 0.2 * roughness(market)
    flat = C.Ctx(ctx.bars, ctx.spot, ctx.edges, ctx.now, ctx.end, scale=ctx.scale, cache=ctx.cache,
                 market=np.full(N, 1 / N), series=tuple(C.Shown(x.end, ctx.edges, np.full(N, 1 / N)) for x in ctx.series))
    assert smoothing.Surface(flat, f).evidence == pytest.approx(0, abs=1e-9)   # an untraded series says nothing


def test_the_kalman_smoother_pulls_one_close_out_of_line_back_towards_its_neighbours():
    hours = np.arange(1, 26, dtype=float)
    du = np.abs(np.diff(np.log(hours)))
    obs = [[(0.5, 0.25)] for _ in hours]
    obs[12] = [(-2.0, 0.25)]                                                # one close's traders say otherwise
    xs, ps = smoothing.rts(obs, du, 0.0, 1.0, smoothing.Q_LEAN)
    assert abs(xs[12] - 0.5) < 0.5 and abs(xs[12] - (-2.0)) > 1.5           # told by the closes either side
    assert np.all(np.abs(np.delete(xs, 12) - 0.5) < 0.2) and np.all(ps > 0) and np.all(ps < 0.25)
    silent = [[] for _ in hours]
    xs0, ps0 = smoothing.rts(silent, du, 0.3, 1.0, smoothing.Q_LEAN)
    assert np.allclose(xs0, 0.3) and np.all(ps0 >= 1.0)                     # nothing seen: the prior, growing less sure


def test_the_band_match_sits_the_fundamental_on_the_surface_band():
    ctx, smooth, market, c = surface_case()
    p = smoothing.band_match(ctx)
    f = benter.fundamental(ctx)
    s = smoothing.Surface(ctx, f)
    ref = smoothing.strip(s.belief())
    want = (1 - s.evidence) * smoothing.quantiles(f, ctx.edge_z, [0.1, 0.9]) + s.evidence * smoothing.quantiles(ref, ctx.edge_z, [0.1, 0.9])
    assert smoothing.quantiles(p, ctx.edge_z, [0.1, 0.9]) == pytest.approx(want, abs=0.03)


def test_shown_series_sorts_the_closes_nearest_first_and_shares_one_ladder():
    bins = [(28_000.0 + 200 * i, 28_200.0 + 200 * i) for i in range(N)]
    got = bots.shown_series([(3000.0, bins, [10.0] * N), (1000.0, list(bins), [10.0] * N), (2000.0, bins, [10.0] * 3)])
    assert [s.end for s in got] == [1000.0, 3000.0]                        # the odd ladder is dropped
    assert got[0].edges is got[1].edges and got[0].edges.shape == (N + 1,)
    assert np.allclose(got[0].probs, 1 / N)


def probe(seen: list) -> bots.Bot:
    """A zoo model that records the series it is handed."""
    def fn(ctx):
        seen.append((ctx.end, ctx.market, ctx.series))
        return bell(242, 4.0)
    m = C.Model("probe", "Probe", "Smoothing", "records the series", "test", fn, reads_market=True, reads_series=True)
    return bots.Bot(m.id, m.name, m.family, m.blurb, m.description, model=m, reads_market=True, reads_series=True)


class Series(FakeApi):
    closes = 3

    def __init__(self, key=None, base_url=None):
        super().__init__(key, base_url)
        self.books = {100 + i: list(self.shares) for i in range(self.closes)}

    def shares_of(self, topic_id):
        return self.books[topic_id]

    async def book(self, topic_id):
        b = await super().book(topic_id)
        b.shares = list(self.books[topic_id])
        b.reprice()
        return b


async def test_the_runner_hands_a_series_bot_every_open_close_fresh_where_it_has_read_them(tmp_path):
    api, seen = Series(), []
    r = runner(api, tmp_path, probe(seen), list_s=3600, bankroll_sats=0.5)    # no budget: paper fills would move the books
    await r.cycle()                                                         # visits the nearest close
    end, market, series = seen[-1]
    assert len(series) == 3 and [s.end for s in series] == sorted(api.ends) and series[0].end == end
    assert market is not None and np.allclose(series[0].probs, market)     # the focused close is its own book
    api.books[101][90] += 400                                               # other traders move the second close
    api.books[102][90] += 400                                               # and the third
    await r.cycle()                                                         # visits the second close, fresh from its book
    end, market, series = seen[-1]
    assert series[1].end == end and series[1].probs[90] > 10 * series[0].probs[90]
    assert series[2].probs[90] == pytest.approx(series[0].probs[90])        # the third is still as the listing had it
    await r.cycle()                                                         # the third, read now
    _, _, series = seen[-1]
    assert series[2].probs[90] > 10 * series[0].probs[90]
    await r.cycle()                                                         # round again: the first, with the others as last seen
    _, _, series = seen[-1]
    assert series[1].probs[90] > 10 * series[0].probs[90] and series[2].probs[90] > 10 * series[0].probs[90]


def test_a_bot_that_reads_only_its_close_is_handed_nothing(tmp_path):
    b = next(x for x in bots.discover() if x.id == "benter")
    ctx = ctx_for(synthetic_bars(), 2.0)
    from glimpse_tui.api import Book
    edges = 28_000.0 + 200.0 * np.arange(N + 1)
    book = Book(1, "m", int(NOW) + 7200, "live", 0, list(range(1, N + 1)), [(float(edges[i]), float(edges[i + 1])) for i in range(N)],
                [40.0] * N, P.alpha_for(N))
    book.reprice()
    b.probs(book, ctx, bots.shown_series([(book.end_time_utc, book.bins, book.shares)]))
    assert ctx.series == () and ctx.market is not None


def test_the_bots_trade_their_own_rules_whatever_the_default_and_every_buy_is_under_value():
    from glimpse_tui.api import Book

    b = next(x for x in bots.discover() if x.id == "smooth_pool")
    cfg = bots.BotConfig(objective="match").with_budget(100_000).for_bot(b)
    assert cfg.objective == "edge" and cfg.kelly == 0.2 and cfg.interval_s == 3 and cfg.edge_close_share == 0.02
    ctx, smooth, market, c = surface_case()
    edges = ctx.edges
    bins = [(float(edges[i]), float(edges[i + 1])) for i in range(N)]
    # a book whose shown curve is the jagged market: shares = b · ln π + level
    alpha = P.alpha_for(N)
    q = np.maximum(np.log(market) - np.log(market).min(), 0) * 600 + 10
    book = Book(1, "m", int(ctx.end), "live", 0, list(range(1, N + 1)), bins, [float(x) for x in q], alpha)
    book.reprice()
    probs = b.probs(book, ctx, ctx.series)
    legs = bots.decide(book, probs, cfg, cfg.max_per_cycle_sats)
    assert legs and sum(x.cost_sats for x in legs) <= 0.02 * 100_000 + 1e-6
    for x in legs:                                                          # each leg, priced as the server fills it, under value
        assert probs[x.index] * PL.NET * x.contracts >= x.cost_sats * (1 + cfg.fill_margin)
    assert any(abs(x.index - (c - 3)) <= 1 for x in legs)                   # the dip is among what it buys
    assert all(x.index != c + 6 for x in legs)                              # the spike is not
