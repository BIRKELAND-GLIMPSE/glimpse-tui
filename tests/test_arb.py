"""The Arbitrageurs: the covering maths, and the runner against a fake API of Bitcoin's and gold's series. No network, no key,
no orders."""
import time

import numpy as np
import pytest
from test_bots import BINS, Feed, make, until  # noqa: F401  (make: the screen fixture)

from glimpse_tui import api, arb, bots, botsview
from glimpse_tui import policy as PL
from glimpse_tui import pricing as P
from glimpse_tui.api import Batch, Book, Fill, MarketRow

SPOT = 76_500.0
K = 103.2e8                                   # oil at $103.20, in sats: WTI/BTC × BTC/USD
GOLD_SPOT = 3_945.0                           # an ounce in dollars
XAUT_SPOT = GOLD_SPOT * 1e8 / SPOT            # an ounce in sats
HOURLY = [(25_000.0 + 200 * i, 25_200.0 + 200 * i) for i in range(500)]      # the hourly Bitcoin ladder
DAILY = BINS                                                                   # the fixture's: 0–500,000 in $1,000 steps
WTI = [(25_000.0 + 1000 * i, 26_000.0 + 1000 * i) for i in range(250)]       # WTI/BTC in sats a barrel
GOLD = [(3_000.0 + 10 * i, 3_010.0 + 10 * i) for i in range(500)]            # hourly gold in dollars an ounce
GOLD_D = [(2_500.0 + 15 * i, 2_515.0 + 15 * i) for i in range(500)]          # daily gold
XAUT = [(2_500_000.0 + 10_000 * i, 2_510_000.0 + 10_000 * i) for i in range(500)]   # Tether Gold in sats an ounce


def edges(bins):
    return np.array([bins[0][0]] + [hi for _, hi in bins], dtype=float)


def at(bins, x: float) -> int:
    return int(np.clip(np.searchsorted(edges(bins), x, side="right") - 1, 0, len(bins) - 1))


def flat(bins, seed: float = 80.0) -> list[float]:
    return [seed] * len(bins)


def sharp(bins, centre: float, width: float = 1.2, height: float = 500.0, seed: float = 80.0) -> list[float]:
    """A book its traders have formed: shares peaking `height` above the seed around the range holding `centre`."""
    c = at(bins, centre)
    return [seed + height * float(np.exp(-0.5 * ((i - c) / width) ** 2)) for i in range(len(bins))]


def book(topic: int, bins, shares, end: int = 0) -> Book:
    b = Book(topic, "m", end, "live", 0, list(range(1, len(bins) + 1)), list(bins), list(shares), P.alpha_for(len(bins)))
    b.reprice()
    return b


def bell(bins, centre: float = SPOT, sd: float = 1_500.0) -> list[float]:
    w = [np.exp(-0.5 * (((lo + hi) / 2 - centre) / sd) ** 2) + 1e-12 for lo, hi in bins]
    return [x / sum(w) for x in w]


def holds(cov: arb.Cover, bins_a, bins_b, link: arb.Link, samples: int = 20_000) -> None:
    """Brute force: for prices all over the band and every way the linked leg can sit inside its slack, at least one
    bought range holds the settlement. Prices on an edge are checked against both ranges they could fall in."""
    rng = np.random.default_rng(3)
    L, U = cov.band
    xs = np.concatenate([rng.uniform(L, U, samples), edges(bins_a)[(edges(bins_a) > L) & (edges(bins_a) < U)], [L, U]])
    ea, eb = edges(bins_a), edges(bins_b)
    sa, sb = set(cov.a), set(cov.b)
    for x in xs:
        for f in (1 - link.band, 1 - link.band / 2, 1.0, 1 + link.band / 2, 1 + link.band):
            y = link.k * f / x if link.kind == "inverse" else link.k * x * f
            ia = {int(np.searchsorted(ea, x, side="right") - 1), int(np.searchsorted(ea, x, side="left") - 1)}
            ib = {int(np.searchsorted(eb, y, side="right") - 1), int(np.searchsorted(eb, y, side="left") - 1)}
            on_a = all(i in sa for i in ia if 0 <= i < len(bins_a)) and any(0 <= i < len(bins_a) for i in ia)
            on_b = all(i in sb for i in ib if 0 <= i < len(bins_b)) and any(0 <= i < len(bins_b) for i in ib)
            assert on_a or on_b, f"x={x:.0f} f={f} lands in A {ia} / B {ib}, neither bought"


# ── the maths ───────────────────────────────────────────────

def test_the_link_maps_both_ways_and_the_slack_shrinks_what_the_other_ladder_can_cover_for_certain():
    inv = arb.Link("inverse", K, 0.01)
    lo, hi = inv.image(76_000, 77_000)
    assert lo == pytest.approx(K * 0.99 / 77_000) and hi == pytest.approx(K * 1.01 / 76_000)
    assert inv.inside(lo, hi) == (pytest.approx(76_000), pytest.approx(77_000))     # inside undoes image exactly
    a, b = inv.inside(K / 77_000, K / 76_000)                      # the same B span without the slack: 1% each way eats it whole
    assert a > b
    a, b = arb.Link("inverse", K, 0.001).inside(K / 77_000, K / 76_000)
    assert 76_000 < a < b < 77_000                                 # a tenth of that leaves the middle surely inside
    same = arb.Link("same", 1.0, 0.001)
    assert same.image(100.0, 200.0) == (pytest.approx(99.9), pytest.approx(200.2))
    assert same.back(*same.image(100.0, 200.0)) == (pytest.approx(100 * 0.999 / 1.001), pytest.approx(200 * 1.001 / 0.999))
    k = 1e8 / SPOT                                                 # gold in sats from gold in dollars
    direct = arb.Link("direct", k, 0.01)
    lo, hi = direct.image(3_900.0, 4_000.0)
    assert lo == pytest.approx(k * 3_900 * 0.99) and hi == pytest.approx(k * 4_000 * 1.01)
    assert direct.inside(lo, hi) == (pytest.approx(3_900), pytest.approx(4_000))
    assert direct.back(lo, hi) == (pytest.approx(3_900 * 0.99 / 1.01), pytest.approx(4_000 * 1.01 / 0.99))


@pytest.mark.parametrize("kind", ["inverse", "same"])
def test_a_cover_holds_the_settlement_wherever_it_lands_in_the_band(kind):
    bins_a, spot = HOURLY, SPOT
    if kind == "inverse":
        bins_b, link = WTI, arb.Link("inverse", K, 0.012)
    elif kind == "direct":
        bins_a, spot, bins_b, link = GOLD, GOLD_SPOT, XAUT, arb.Link("direct", 1e8 / SPOT, 0.012)    # gold in dollars, gold in sats
    else:
        bins_b, link = DAILY, arb.Link("same", 1.0, 0.001)
    rng = np.random.default_rng(1)
    pa = P.prices(sharp(bins_a, spot, 2.0, 400), P.alpha_for(500))
    pb = P.prices([80 + 200 * rng.random() for _ in bins_b], P.alpha_for(len(bins_b)))   # a noisy book: a jagged cover
    band = (spot * 0.93, spot * 1.07)
    cov = arb.cover(pa, edges(bins_a), pb, edges(bins_b), link, band)
    assert cov is not None and cov.a and cov.b                      # both ladders take part
    holds(cov, bins_a, bins_b, link)
    # never dearer than either ladder doing the whole job
    ea = edges(bins_a)
    all_a = sum(pa[j] for _, _, j in arb.cells(ea, band) if j is not None)
    need = set()
    for lo, hi, _ in arb.cells(ea, band):
        k1, k2 = arb.reach(link, lo, hi, edges(bins_b))
        need.update(range(k1, k2 + 1))
    assert cov.marginal <= all_a + 1e-9 and cov.marginal <= sum(pb[k] for k in need) + 1e-9


def test_a_band_past_both_ladders_cannot_be_covered_and_one_past_only_bitcoins_is_covered_by_the_other():
    link = arb.Link("inverse", K, 0.005)
    pa, pb = P.prices(flat(HOURLY), P.alpha_for(500)), P.prices(flat(WTI), P.alpha_for(250))
    assert arb.cover(pa, edges(HOURLY), pb, edges(WTI), link, (20_000.0, 30_000.0)) is None    # below 25k: no ladder
    cov = arb.cover(pa, edges(HOURLY), pb, edges(WTI), link, (120_000.0, 140_000.0))           # past 125k: WTI/BTC alone
    assert cov is not None and cov.b and all(HOURLY[j][0] < 125_000 for j in cov.a)
    holds(cov, HOURLY, WTI, link, samples=2_000)


def test_the_arbitrage_appears_when_the_books_disagree_and_not_when_they_agree():
    link = arb.Link("inverse", K, 0.01)
    a = book(1, HOURLY, sharp(HOURLY, SPOT))                                    # Bitcoin's book has sharpened on spot
    probs = bell(HOURLY)
    # WTI/BTC still near its seed: the cover is cheap, mostly its flat ranges
    b_flat = book(2, WTI, flat(WTI))
    take, seen = arb.best(a, b_flat, probs, link, 0.05, 1e-3)
    assert take is not None and take.gross > 0.05 and take.band[0] < SPOT < take.band[1]
    assert take.tail == 0 and take.band[0] <= 25_000 and take.band[1] >= 400_000    # the whole line: a true arbitrage, taken first
    holds(take, HOURLY, WTI, link, samples=3_000)
    narrow = arb.cover(a.prices, edges(HOURLY), b_flat.prices, edges(WTI), link, (SPOT * 0.97, SPOT * 1.03))
    assert narrow.gross > take.gross > 0.05                                   # a narrower band pays more, but is not risk-free
    # WTI/BTC formed, but on a different price: each book covers where the other is confident
    b_wrong = book(3, WTI, sharp(WTI, K / (SPOT * 1.025), 1.0, 500))
    take2, _ = arb.best(a, b_wrong, probs, link, 0.05, 1e-3)
    assert take2 is not None and take2.a and take2.b
    assert any(HOURLY[j][0] >= SPOT * 1.015 for j in take2.a)                  # Bitcoin's ladder bought where WTI/BTC is sure
    holds(take2, HOURLY, WTI, link, samples=3_000)
    # both formed on the same price: no cover pays
    b_agree = book(4, WTI, sharp(WTI, K / SPOT, 1.0, 500))
    take3, seen3 = arb.best(a, b_agree, probs, link, 0.05, 1e-3)
    assert take3 is None and seen3 is not None and seen3.gross < 0.05


def test_sizing_stops_where_the_lifted_books_no_longer_return_the_edge_and_within_the_room():
    link = arb.Link("inverse", K, 0.01)
    a, b = book(1, HOURLY, sharp(HOURLY, SPOT)), book(2, WTI, flat(WTI))
    take, _ = arb.best(a, b, bell(HOURLY), link, 0.05, 1e-3)
    d, cost = arb.size(a, b, take, 20_000, 0.05)
    assert d >= 1 and 0 < cost <= 20_000 and PL.NET * d >= 1.05 * cost
    assert arb.price(a, b, take, d) == pytest.approx(cost)
    more = arb.price(a, b, take, d * 1.5)
    assert PL.NET * d * 1.5 < 1.05 * more or more > 20_000                   # half again would break the rule
    small, c_small = arb.size(a, b, take, 50, 0.05)
    assert 0 < c_small <= 50 and small < d
    assert arb.size(a, b, take, 0.5, 0.05) == (0.0, 0.0)


def test_bands_start_with_the_whole_line_then_narrow_by_consensus_tail_and_only_a_formed_book_widens_them():
    link = arb.Link("inverse", K, 0.01)
    a, b = book(1, HOURLY, sharp(HOURLY, SPOT)), book(2, WTI, flat(WTI))
    out = arb.bands(bell(HOURLY), edges(HOURLY), edges(WTI), link, a, b, 1e-3)
    (full, t0), rest = out[0], out[1:]
    assert full[0] <= 25_000 and full[1] >= K * 0.99 / 25_000 and t0 == 0
    widths = [U - L for (L, U), _ in rest]
    assert widths == sorted(widths, reverse=True) and all(0 < t <= 1e-3 for _, t in rest)
    assert arb.formed(b.probs) < arb.MIN_FORMED < arb.formed(a.probs)
    b_formed = book(3, WTI, sharp(WTI, K / (SPOT * 1.05), 1.0, 500))        # a formed book far from the consensus
    wider = arb.bands(bell(HOURLY), edges(HOURLY), edges(WTI), link, a, b_formed, 1e-3)
    assert wider[-1][0][1] > rest[-1][0][1]                                   # its own band pulls the cover out to it


def test_link_sigma_reads_the_free_leg_between_two_ladders_and_the_band_grows_with_the_horizon():
    import pandas as pd

    idx = pd.date_range("2026-01-01", periods=1500, freq="h", tz="UTC")
    rng = np.random.default_rng(0)
    btc = pd.DataFrame({"close": 80_000 * np.exp(np.cumsum(rng.normal(0, 0.004, 1500)))}, index=idx)
    oil = np.exp(np.cumsum(rng.normal(0, 0.005, 1500)))                        # the dollar leg
    wti = pd.DataFrame({"close": 100 * 1e8 * oil / btc["close"].to_numpy()}, index=idx)
    s = arb.link_sigma(btc, wti, "inverse")                                    # oil's dollar price moves between BTC and WTI/BTC
    assert 0.0045 < s < 0.0055
    gold = pd.DataFrame({"close": 4_000 * np.exp(np.cumsum(rng.normal(0, 0.002, 1500)))}, index=idx)
    xaut = pd.DataFrame({"close": gold["close"].to_numpy() * 1e8 / btc["close"].to_numpy()}, index=idx)
    assert 0.0035 < arb.link_sigma(gold, xaut, "direct") < 0.0045            # Bitcoin's price moves between gold in $ and in sats
    assert 0.0015 < arb.link_sigma(btc, xaut, "inverse") < 0.0025            # gold's dollar price between BTC and Tether Gold
    assert arb.link_sigma(gold, gold, "same") == 0.0
    r = arb.Rules()
    assert arb.slack(r, "inverse", s, 0.1) < arb.slack(r, "inverse", s, 1.0) < arb.slack(r, "inverse", s, 4.0)
    assert arb.slack(r, "inverse", s, 1.0) == pytest.approx(r.band_floor + r.dollar_z * s)
    assert arb.slack(r, "same", 0.0, 1.0) == r.same_band
    assert arb.link_sigma(btc.iloc[:50], wti.iloc[:50], "inverse") == arb.SIGMA_DEFAULT


def test_rules_come_from_an_arbitrage_table_in_bots_toml():
    from glimpse_tui import auth

    d = auth.config_dir()                                                      # the test's own XDG_CONFIG_HOME (conftest)
    d.mkdir(parents=True, exist_ok=True)
    (d / "bots.toml").write_text('interval_s = 9\n[arbitrage]\npair_share = 0.1\nmax_tail = 0.0001\nnonsense = 1\n')
    r = arb.Rules.load()
    assert r.pair_share == 0.1 and r.max_tail == 1e-4 and r.dollar_z == arb.Rules().dollar_z
    assert bots.BotConfig.load().interval_s == 9                               # the table does not leak into BotConfig


# ── the runner ──────────────────────────────────────────────

class Exchange:
    """Glimpse's series. The hourly ones (Bitcoin 101/102, WTI/BTC 301/302, gold 401/402, Tether Gold 501/502) close at e1
    and e2; the daily ones (Bitcoin 201, gold 601) close at e1, so e1 is a 00:00 with every pair and e2 has the hourly ones.
    Bitcoin's and gold's dollar books have formed on spot; the books in satoshis (WTI/BTC, Tether Gold) are flat."""

    def __init__(self, key=None, reject: set[int] = ()):
        self.authenticated = bool(key)
        self.e1 = int(time.time()) + 3600
        e2 = self.e1 + 3600
        self.ends = {101: self.e1, 102: e2, 201: self.e1, 301: self.e1, 302: e2, 401: self.e1, 402: e2, 501: self.e1, 502: e2, 601: self.e1}
        self.ladders = {101: HOURLY, 102: HOURLY, 201: DAILY, 301: WTI, 302: WTI, 401: GOLD, 402: GOLD, 501: XAUT, 502: XAUT, 601: GOLD_D}
        self.books = {101: sharp(HOURLY, SPOT), 102: sharp(HOURLY, SPOT, 2.0, 150), 201: sharp(DAILY, SPOT, 1.0, 300),
                      301: flat(WTI), 302: flat(WTI), 401: sharp(GOLD, GOLD_SPOT), 402: sharp(GOLD, GOLD_SPOT, 2.0, 150),
                      501: flat(XAUT), 502: flat(XAUT), 601: sharp(GOLD_D, GOLD_SPOT, 1.0, 300)}
        self.reject = set(reject)
        self.bought, self.sold, self.estimated, self.read = [], [], [], []

    async def batches(self):
        return [Batch("b-h", "Hourly Bitcoin Prediction Markets", 2, 200), Batch("b-d", "Daily Bitcoin Markets", 1, 1000),
                Batch("b-x", "Hourly Gold Prediction Markets", 2, 10), Batch("b-xd", "Daily PAX Gold Market", 1, 15),
                Batch("b-t", "Hourly Tether Gold in sats per troy ounce", 2, 10000), Batch("b-w", "Hourly WTI/BTC Prediction Markets", 2, 500)]

    def _row(self, t: int) -> MarketRow:
        bins = self.ladders[t]
        return MarketRow(t, "m", self.ends[t], "live", 0, 0, tuple(self.books[t]), tuple(f"{lo:.0f}-{hi:.0f}" for lo, hi in bins),
                         tuple(range(1, len(bins) + 1)))

    async def all_markets(self, batch_id):
        first = {"b-h": (101, 102), "b-d": (201,), "b-w": (301, 302), "b-x": (401, 402), "b-xd": (601,), "b-t": (501, 502)}[batch_id]
        return [self._row(t) for t in first]

    async def book(self, topic_id):
        self.read.append(topic_id)
        return book(topic_id, self.ladders[topic_id], self.books[topic_id], self.ends[topic_id])

    async def estimate_legs(self, topic_id, legs):
        self.estimated.append(topic_id)
        q, a = self.books[topic_id], P.alpha_for(len(self.books[topic_id]))
        q2 = q[:]
        for o, c in legs:
            q2[o - 1] += c
        raw = P.cost(q2, a) - P.cost(q, a)
        return raw, raw * P.FEE

    async def buy_many(self, groups):
        out = []
        for t, legs in groups:
            if t in self.reject:
                out.append(Fill(t, "", 0.0, 0.0, error="insufficient liquidity"))
                continue
            raw, fee = await self.estimate_legs(t, legs)
            for o, c in legs:
                self.books[t][o - 1] += c
            self.bought.append((t, legs))
            out.append(Fill(t, "t", raw, max(fee, 1.0)))
        return out

    async def sell_many(self, groups):
        out = []
        for t, legs in groups:
            got, a = 0.0, P.alpha_for(len(self.books[t]))
            for o, c in legs:
                self.sold.append((t, o, c))
                got += P.sell_proceeds(self.books[t], a, o - 1, c)
                self.books[t][o - 1] -= c
            out.append(Fill(t, "t", got, got * P.FEE / (1 - P.FEE)))
        return out

    async def positions(self):
        return []


class OilFeed(Feed):
    """WTI/BTC candles that are oil at a fixed dollar price over Bitcoin's, so the implied dollar leg barely moves."""
    asset = "WTIBTC"

    def __init__(self):
        super().__init__()
        self.bars = self.bars.assign(**{c: K / self.bars[c] for c in ("open", "high", "low", "close")})
        self.spot = K / SPOT


class GoldFeed(Feed):
    """Gold in dollars: a third as volatile as Bitcoin and moving with it, so gold in sats is not a constant."""
    asset = "XAU"

    def __init__(self):
        super().__init__()
        self.bars = self.bars.assign(**{c: GOLD_SPOT * (self.bars[c] / SPOT) ** 0.3 for c in ("open", "high", "low", "close")})
        self.spot = GOLD_SPOT


class XautFeed(Feed):
    """Tether Gold in sats an ounce: gold in dollars × 10⁸ over Bitcoin in dollars, bar by bar."""
    asset = "XAUTBTC"

    def __init__(self):
        super().__init__()
        gold = GoldFeed().bars
        self.bars = self.bars.assign(**{c: gold[c] * 1e8 / self.bars[c] for c in ("open", "high", "low", "close")})
        self.spot = XAUT_SPOT


def feeds(asset: str):
    return {"WTIBTC": OilFeed, "XAU": GoldFeed, "XAUTBTC": XautFeed}.get(asset, Feed)()


def arbitrageur(scheme: str = "bitcoin") -> bots.Bot:
    def forecast(book, closes, hours):
        return bell(book.bins, closes[-1] if book.bins[0][0] < closes[-1] < book.bins[-1][1] else (book.bins[0][0] + book.bins[-1][1]) / 2,
                    (book.bins[-1][1] - book.bins[0][0]) / 60)
    return bots.Bot("arb", "Arb", "Arbitrage", "covers", "test", forecast=forecast, arbitrage=scheme)


def runner(api, tmp_path, live=False, budget=100_000, scheme="bitcoin", **rules):
    return arb.Arbitrageur(bot=arbitrageur(scheme), api=api, batch_id="", feed=None, live=live, make_feed=feeds,
                           ledger=bots.Ledger(tmp_path / "ledger.json"), cfg=bots.BotConfig(interval_s=0, min_edge=0.05).with_budget(budget),
                           rules=arb.Rules(**rules))


async def test_on_paper_it_covers_the_nearest_pair_within_its_share_and_the_lifted_books_stop_it_repeating(tmp_path):
    api = Exchange()
    r = runner(api, tmp_path)
    await r.cycle()
    assert api.bought == [] and r.trades == 1
    log = "\n".join(r.log)
    assert "1/4  BTC·WTIBTC at" in log and "would buy cover" in log and "pays" in log and "of the consensus outside" in log
    held = r.holdings()
    assert {x[2] for x in held} <= {101, 301} and 301 in {x[2] for x in held}      # the flat WTI/BTC ranges carry the cover
    assert 0 < r.open_cost <= 0.2 * 100_000 + 1e-6                                  # the pair's share of the budget
    first = r.open_cost
    for _ in range(6):
        await r.cycle()
    assert first <= r.open_cost <= 100_000 + 1e-6                                   # other pairs covered too; never past the budget
    pair = r.ledger.open_cost("arb", "paper", 101) + r.ledger.open_cost("arb", "paper", 301)
    assert pair <= 0.2 * 100_000 + r.ledger.open_cost("arb", "paper", 101) + 1e-6   # the pair's share, 101's share with BTC 1D aside
    assert any("full share" in x or "needs" in x or "no size" in x for x in list(r.log)[2:])  # its paper fills lift its own books
    assert bots.Ledger(tmp_path / "ledger.json").open_cost("arb", "paper") == pytest.approx(r.open_cost)
    assert "covers coinciding closes" in botsview.log_title(None, r) and "a pair at most" in botsview.portfolio(r, "BTC", 120).plain


async def test_it_visits_the_nearest_pair_every_other_time_and_walks_every_pair_in_between(tmp_path):
    api = Exchange()
    r = runner(api, tmp_path, pair_share=0.0)                                       # no room: visits only
    for _ in range(8):
        await r.cycle()
    pairs = [tuple(api.read[i:i + 2]) for i in range(0, len(api.read), 2)]
    assert pairs[::2] == [(101, 301)] * 4                                           # the nearest, every other visit
    assert pairs[1::2] == [(101, 301), (101, 201), (201, 301), (102, 302)]          # the round: three pairs at e1, one at e2
    assert r.of == 4 and all("holds ₿0 in this pair" in x for x in r.log if "cover at" in x)


async def test_live_it_buys_both_legs_in_one_request_after_the_servers_estimate(tmp_path):
    api = Exchange("k")
    r = runner(api, tmp_path, live=True)
    await r.cycle()
    assert r.trades == 1 and api.bought and {t for t, _ in api.bought} <= {101, 301} and 301 in {t for t, _ in api.bought}
    assert set(api.estimated) >= {t for t, _ in api.bought}
    held = r.ledger.legs("arb", "live", 301)
    assert held and all(n == pytest.approx(api.bought[-1][1][0][1]) for n, _ in held.values())    # the same size on every range
    assert sum(c for _, c in held.values()) + sum(c for _, c in r.ledger.legs("arb", "live", 101).values()) == pytest.approx(r.paid_sats)
    assert any("BUY cover" in x for x in r.log)


async def test_live_a_leg_the_server_fills_alone_is_sold_straight_back(tmp_path):
    api = Exchange("k", reject={301})
    r = runner(api, tmp_path, live=True)
    # make WTI/BTC formed on another price so the cover needs both legs
    api.books[301] = sharp(WTI, K / (SPOT * 1.025), 1.0, 500)
    await r.cycle()
    log = "\n".join(r.log)
    assert "one leg rejected" in log and "selling the other back" in log and "sold BTC back" in log
    assert api.bought and all(t == 101 for t, _ in api.bought) and api.sold and all(t == 101 for t, _, _ in api.sold)
    assert r.holdings() == [] and r.trades == 0 and r.sold_sats > 0


async def test_a_moved_book_is_skipped_live(tmp_path, monkeypatch):
    api = Exchange("k")
    r = runner(api, tmp_path, live=True)

    async def dear(topic_id, legs):
        raw, fee = await Exchange.estimate_legs(api, topic_id, legs)
        return raw * 1.3, fee * 1.3
    monkeypatch.setattr(api, "estimate_legs", dear)
    await r.cycle()
    assert api.bought == [] and any("book moved" in x for x in r.log)


def test_each_is_a_bot_of_its_own_that_plans_no_single_close_bet():
    found = {b.id: b for b in bots.discover()}
    b, g = found["arbitrage"], found["arbitrage_gold"]
    assert b.arbitrage == "bitcoin" and g.arbitrage == "gold" and arb.scheme_of(b) is arb.BITCOIN and arb.scheme_of(g) is arb.GOLD
    assert {b.family, g.family} == {"Arbitrage"} and b.policy is g.policy is None and not b.sells_only and not g.sells_only
    bk = book(0, HOURLY, sharp(HOURLY, SPOT))
    assert bots.plan(b, bk, bell(HOURLY), 100_000, SPOT) == [] and bots.plan(g, bk, bell(HOURLY), 100_000, SPOT) == []
    runner_note = dict(botsview.sections(b))["runner"]
    assert "never backs a direction" in runner_note and "sold straight back" in runner_note and "WTI/BTC every hour" in runner_note
    gold_note = dict(botsview.sections(g))["runner"]
    assert "Tether Gold" in gold_note and "oil/gold ratio" in gold_note and "sold straight back" in gold_note
    assert arb.GOLD.series == ("XAU", "XAU 1D", "XAUTBTC", "BTC", "WTIBTC") and arb.GOLD.label.endswith("BTC and WTIBTC")


# ── gold ────────────────────────────────────────────────────

def test_tether_gold_in_sats_is_a_series_of_its_own_priced_by_the_oracle():
    assert api.asset_of("Hourly Tether Gold in sats per troy ounce") == "XAUTBTC"
    assert api.asset_of("Hourly XAUT/BTC Markets") == "XAUTBTC"
    assert api.asset_of("Hourly Gold Prediction Markets") == "XAU" and api.asset_of("Daily PAX Gold Market") == "XAU"
    assert Batch("t", "Hourly Tether Gold in sats per troy ounce", 1, 10000).short == "XAUTBTC"
    assert Batch("x", "Hourly Gold Prediction Markets", 1, 10).short == "XAU" and Batch("d", "Daily PAX Gold Market", 1, 15).short == "XAU 1D"
    assert "XAUTBTC" in api.ORACLE and "XAUTBTC" in api.HOURLY and "XAUTBTC" in api.ASSETS and "XAUTBTC" not in api.PAIRS


async def test_the_gold_arbitrageur_pairs_every_gold_ladder_with_what_links_it_and_walks_them_in_order(tmp_path):
    api_ = Exchange()
    r = runner(api_, tmp_path, scheme="gold", pair_share=0.0)                      # no room: visits only
    for _ in range(16):
        await r.cycle()
    pairs = [tuple(api_.read[i:i + 2]) for i in range(0, len(api_.read), 2)]
    assert pairs[::2] == [(401, 501)] * 8                                           # gold in dollars with gold in sats, nearest
    assert pairs[1::2] == [(401, 501), (101, 501), (501, 301), (401, 601), (601, 501), (402, 502), (102, 502), (502, 302)]
    assert r.of == 8 and r.series == "XAU · XAU 1D · XAUTBTC · BTC · WTIBTC"
    log = "\n".join(r.log)
    assert "XAU·XAUTBTC at" in log and "BTC·XAUTBTC at" in log and "XAUTBTC·WTIBTC at" in log and "XAU·XAU 1D at" in log
    assert all("holds ₿0 in this pair" in x for x in r.log if "cover at" in x)


async def test_on_paper_the_gold_arbitrageur_covers_gold_in_dollars_with_the_flat_book_in_sats(tmp_path):
    api_ = Exchange()
    r = runner(api_, tmp_path, scheme="gold")
    await r.cycle()
    assert api_.bought == [] and r.trades == 1
    log = "\n".join(r.log)
    assert "1/8  XAU·XAUTBTC at" in log and "would buy cover" in log and "XAUTBTC" in log.split("would buy cover")[1]
    assert "slack on the XAUTBTC leg" in log
    held = {x[2] for x in r.holdings()}
    assert held <= {401, 501} and 501 in held                                       # the flat sats-per-ounce ranges carry the cover
    assert 0 < r.open_cost <= 0.2 * 100_000 + 1e-6
    for _ in range(7):
        await r.cycle()
    assert r.open_cost <= 100_000 + 1e-6 and {x[2] for x in r.holdings()} & {101, 301, 601}      # the other pairs covered too


async def test_live_the_gold_arbitrageur_buys_both_legs_in_one_request_and_two_agreeing_books_give_it_nothing(tmp_path):
    api_ = Exchange("k")
    r = runner(api_, tmp_path, live=True, scheme="gold")
    await r.cycle()                                                                 # XAU·XAUTBTC: gold in dollars formed, in sats flat
    assert r.trades == 1 and {t for t, _ in api_.bought} <= {401, 501} and 501 in {t for t, _ in api_.bought}
    assert set(api_.estimated) >= {t for t, _ in api_.bought}
    assert any("BUY cover" in x and "XAU·XAUTBTC" in x and "slack on the XAUTBTC leg" in x for x in r.log)
    # a sats-per-ounce book formed on the same price as the dollar one: nothing to cover
    api_ = Exchange("k")
    api_.books[501] = sharp(XAUT, XAUT_SPOT, 1.0, 500)
    r = runner(api_, tmp_path, live=True, scheme="gold")
    await r.cycle()
    assert api_.bought == [] and r.trades == 0 and any("needs" in x or "no band" in x for x in r.log)


async def test_enter_runs_the_arbitrageur_on_paper_across_the_three_series(make):  # noqa: F811
    app = make()
    async with app.run_test(size=(170, 46)) as pilot:
        await until(pilot, lambda: app.views)
        await pilot.press("B")
        await until(pilot, lambda: app.bots and not app.bot_scanning and len(app.bot_scan) == len(app.bots))
        await pilot.press("slash", *"arbitrageur", "enter")
        await pilot.press("b")
        await pilot.pause()
        assert "two closes at once" in app.flash
        await pilot.press("enter", "enter")                            # the budget prompt, then its default; no key: paper
        await until(pilot, lambda: "arbitrage" in app.runners and app.runners["arbitrage"].cycles >= 1)
        r = app.runners["arbitrage"]
        assert isinstance(r, arb.Arbitrageur) and not r.live
        assert "covers coinciding closes" in app.query_one("#botlog").border_title
        await until(pilot, lambda: any("no two closes" in x for x in r.log))      # the fake API lists one series only
        await pilot.press("X")
