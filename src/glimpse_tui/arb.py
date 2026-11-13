"""The Arbitrageur: one close, two series, both sides bought for less than they pay, whichever way the price goes.

Three of Glimpse's series settle on Bitcoin's price at the same instants. Every hourly WTI/BTC close has an hourly
Bitcoin close at the same second (oil in satoshis a barrel is oil in dollars over Bitcoin in dollars, so once the oil
leg is pinned the two ladders describe one event), and at 00:00 UTC the daily Bitcoin close joins them. Two books
on one event should agree. They are made by different traders at different speeds, and they don't: minutes before a
close the hourly Bitcoin book has put half its weight on one range while the WTI/BTC book still sits near the flat
subsidy it was seeded with.

A cover buys, in the two books together, a set of ranges that holds the settlement price wherever it lands: each
Bitcoin price in the band either sits in a range bought on the Bitcoin ladder, or every WTI/BTC range it can settle
in (the oil leg's possible move allowed for) is bought on the other. Whichever way the price moves, at least one
contract pays 98 sats. If the two legs cost less than that after the buying fee, the difference is locked in at the
moment of purchase. The bot takes the cover when it is there and holds it to the close. It has no view on direction.

The cheapest cover is found exactly: the band is cut into the Bitcoin ladder's ranges, each is either bought or
handed to the other book, which must then hold every range its image can reach, and since those images move one
way along the ladder a dynamic programme over "the highest range bought so far on the other side" finds the least
total at today's marginal prices (`cover`). The cover is then priced at size with the LS-LMSR cost function itself,
both fees in, and sized to the largest number of contracts that still returns `min_edge` on what it costs (`size`).

The band is the honest part. On LS-LMSR a flat ladder's prices sum to three times the payout, so covering the whole
price line (both ladders end to end) rarely costs under 98 sats, though the bot checks it first and takes it when it
does: that is a true arbitrage. Otherwise it covers the widest band of the price line that still locks the return,
chosen so that the zoo's consensus of how far Bitcoin moves (fat-tailed, seven volatility and jump models) leaves at
most `max_tail` (a tenth of a percent by default) outside it. Inside the band the result does not depend on where the
price goes; outside it the stake is lost. The log says, on every visit, what the cover costs, what it pays and how much
the consensus leaves outside.

The bot comes in schemes, one per family of linked series (`SCHEMES`). The Arbitrageur covers BTC, BTC 1D and WTIBTC.
The Gold Arbitrageur covers XAU, XAU 1D and XAUTBTC (Tether Gold in satoshis a troy ounce) against each other and
against BTC and WTIBTC. The `triangle` scheme (BTC, XAU, XAUTBTC) belongs to the Triangulator in `tri.py`, which
reads the three books at one instant together and trades all three toward one consistent picture; it uses the
covering maths here for the risk-free part of what it finds. Any two ladders whose settlements are one price (`same`),
proportional (`direct`, B = k·A: gold in dollars against gold in sats, oil in sats against gold in sats) or reciprocal
(`inverse`, B = k/A: Bitcoin in dollars against anything in sats) can be paired; k is read from the two spots and its
slack from the two series' candles.
"""
from __future__ import annotations

import asyncio
import math
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field, fields

import numpy as np

from . import fmt
from . import policy as PL
from . import pricing as P
from .api import ApiError, Batch, Book, MarketRow
from .auth import config_dir
from .bots import Picture, Runner, listing, off_loop

TAILS = (1e-5, 1e-4, 1e-3)                 # the bands tried after the whole price line, by consensus mass left outside
SIGMA_DEFAULT = 0.006                      # hourly σ of the free leg between two ladders until the candles say otherwise (oil's)
SIGMA_FLOOR = 0.002
MIN_FORMED = 0.05                          # a book less formed than this has no view of its own to widen the band to


@dataclass(frozen=True)
class Rules:
    """How the Arbitrageur trades. Its own, read from a `[arbitrage]` table in ~/.config/glimpse/bots.toml."""
    pair_share: float = 0.20        # most of the budget held in one pair of closes
    max_tail: float = 1e-3          # most probability the consensus may leave outside the covered band
    same_band: float = 1e-3         # slack between two Bitcoin markets' settlement prices (oracles, timing)
    dollar_z: float = 6.0           # the free leg's band: this many hourly σ of what moves between the two ladders, √h scaled...
    band_floor: float = 3e-3        # ...plus this, for the spot ratio being read from two feeds

    @classmethod
    def load(cls, **override: float) -> Rules:
        f = config_dir() / "bots.toml"
        raw = tomllib.loads(f.read_text()).get("arbitrage", {}) if f.is_file() else {}
        known = {x.name for x in fields(cls)}
        return cls(**{k: v for k, v in {**raw, **override}.items() if k in known})


@dataclass(frozen=True)
class Rule:
    """One kind of pair a scheme trades: series `a`, whose ladder the consensus is drawn on, with series `b`, and how
    B's settlement follows A's: `same` (one price, two markets), `direct` (B = k·A) or `inverse` (B = k/A)."""
    a: str
    b: str
    kind: str


@dataclass(frozen=True)
class Scheme:
    """A family of linked series and the pairs of coinciding closes traded across them, in the order they are visited
    at one instant. `name` is how the texts call what settles; `pairs_text` and `free_text` are its own words for the
    about screen."""
    id: str
    name: str
    series: tuple[str, ...]
    rules: tuple[Rule, ...]
    pairs_text: str
    free_text: str

    @property
    def label(self) -> str:
        return ", ".join(self.series[:-1]) + " and " + self.series[-1]


BITCOIN = Scheme(
    "bitcoin", "Bitcoin", ("BTC", "BTC 1D", "WTIBTC"),
    (Rule("BTC", "WTIBTC", "inverse"), Rule("BTC", "BTC 1D", "same"), Rule("BTC 1D", "WTIBTC", "inverse")),
    "hourly Bitcoin with WTI/BTC every hour; at 00:00 UTC the daily Bitcoin close with each of them",
    "the oil leg (WTI in dollars, what moves between Bitcoin in dollars and oil in satoshis)",
)
GOLD = Scheme(
    "gold", "gold", ("XAU", "XAU 1D", "XAUTBTC", "BTC", "WTIBTC"),
    (Rule("XAU", "XAUTBTC", "direct"), Rule("BTC", "XAUTBTC", "inverse"), Rule("XAUTBTC", "WTIBTC", "direct"),
     Rule("XAU", "XAU 1D", "same"), Rule("XAU 1D", "XAUTBTC", "direct")),
    "hourly gold in dollars with Tether Gold in satoshis every hour, Tether Gold with hourly Bitcoin and with WTI/BTC every "
    "hour; at 00:00 UTC the daily gold close with the hourly one and with Tether Gold",
    "the free leg between the two ladders (Bitcoin's price between gold in dollars and gold in satoshis, gold's dollar price "
    "between Bitcoin and Tether Gold, the oil/gold ratio between WTI/BTC and Tether Gold)",
)
TRIANGLE = Scheme(
    "triangle", "gold in satoshis", ("BTC", "XAU", "XAUTBTC"),
    (Rule("XAU", "XAUTBTC", "direct"), Rule("BTC", "XAUTBTC", "inverse")),
    "hourly Bitcoin, hourly gold in dollars and Tether Gold in satoshis, the three closes at one instant together",
    "the leg the two input ladders leave free (Bitcoin's price between gold in dollars and gold in satoshis, gold's dollar "
    "price between Bitcoin and Tether Gold)",
)
OIL = Scheme(
    "oil", "oil in satoshis", ("BTC", "WTIBTC"),
    (Rule("BTC", "WTIBTC", "inverse"),),
    "hourly Bitcoin with WTI/BTC every hour, the two closes at one instant together with a model of oil's dollar price",
    "the oil leg (WTI in dollars, what moves between Bitcoin in dollars and oil in satoshis)",
)
SCHEMES = {s.id: s for s in (BITCOIN, GOLD, TRIANGLE, OIL)}


def scheme_of(bot) -> Scheme:
    """The scheme a zoo entry names in `Model.arbitrage` (`"bitcoin"`, `"gold"`, `"triangle"`); a bare True is the
    Bitcoin one. The triangle is run by `glimpse_tui.tri.Triangulator`, which trades its three closes at once."""
    return SCHEMES.get(str(getattr(bot, "arbitrage", "")), BITCOIN)


# ── how the two ladders relate ──────────────────────────────

@dataclass(frozen=True)
class Link:
    """How market B's settlement price follows market A's: `same` (both settle on one price), `direct` (B = k·A: gold in
    sats an ounce is gold in dollars × 10⁸ over Bitcoin in dollars, k that factor) or `inverse` (B = k / A: WTI/BTC in
    sats a barrel is WTI in dollars × 10⁸ over Bitcoin in dollars, k that dollar price in sats). `band` is the relative
    slack in the relation by the close: the free leg's move, and any difference between the oracles."""
    kind: str
    k: float = 1.0
    band: float = 0.0

    def image(self, lo: float, hi: float) -> tuple[float, float]:
        """Every B price an A price in [lo, hi] can settle at. The daily ladders' first range starts at 0, where the
        inverse is unbounded: no B ladder reaches it, so that range can only be bought on A."""
        if self.kind != "inverse":
            return self.k * lo * (1 - self.band), self.k * hi * (1 + self.band)
        return self.k * (1 - self.band) / hi, (self.k * (1 + self.band) / lo if lo > 0 else float("inf"))

    def inside(self, lo: float, hi: float) -> tuple[float, float]:
        """Every A price whose whole image lies within the B prices [lo, hi]: what B can cover for certain."""
        if self.kind != "inverse":
            return lo / (self.k * (1 - self.band)), hi / (self.k * (1 + self.band))
        return self.k * (1 + self.band) / hi, self.k * (1 - self.band) / lo

    def back(self, lo: float, hi: float) -> tuple[float, float]:
        """Every A price a B price in [lo, hi] can come from."""
        if self.kind != "inverse":
            return lo / (self.k * (1 + self.band)), hi / (self.k * (1 - self.band))
        return self.k * (1 - self.band) / hi, (self.k * (1 + self.band) / lo if lo > 0 else float("inf"))


def link_sigma(bars_a, bars_b, kind: str) -> float:
    """Hourly σ of the log of what moves freely between the two ladders, from their candles: B × A for an inverse
    link (the dollar price of oil, from WTI/BTC × BTC/USD), B / A for a direct one (Bitcoin's price, from gold in sats
    over gold in dollars), nothing for two markets on one price. The default until both feeds share 200 hours."""
    if kind == "same":
        return 0.0
    try:
        j = bars_b["close"].to_frame("b").join(bars_a["close"].to_frame("a"), how="inner")
        a, b = j["a"].to_numpy(dtype=float), j["b"].to_numpy(dtype=float)
        r = np.diff(np.log(b * a if kind == "inverse" else b / a))
        r = r[np.isfinite(r)]
        if r.size < 200:
            return SIGMA_DEFAULT
        return float(max(np.std(r[-1000:]), SIGMA_FLOOR))
    except Exception:
        return SIGMA_DEFAULT


def slack(rules: Rules, kind: str, sigma: float, hours: float) -> float:
    """The link's relative band to a close `hours` out: `dollar_z` σ of the free leg's hourly move, √h scaled, plus the
    floor (`same_band` between two markets on one price, else `band_floor` for the spots read from two feeds)."""
    floor = rules.same_band if kind == "same" else rules.band_floor
    return floor + rules.dollar_z * sigma * math.sqrt(max(hours, 1 / 60))


# ── the cover ───────────────────────────────────────────────

@dataclass(frozen=True)
class Cover:
    """What to buy: `a` the A ranges, `b` the B ranges, for `band` of A prices; `marginal` sats a contract at today's
    marginal prices before fees; `tail` the consensus probability left outside the band."""
    band: tuple[float, float]
    a: tuple[int, ...]
    b: tuple[int, ...]
    marginal: float
    tail: float = 0.0

    @property
    def gross(self) -> float:
        """What a contract of the cover returns on its marginal cost, after both fees: 98 / (1.02 × cost) − 1."""
        return PL.NET / ((1 + P.FEE) * self.marginal) - 1 if self.marginal > 0 else 0.0


def formed(probs) -> float:
    """0 for a flat ladder (nothing traded but the subsidy), towards 1 as a book's curve takes a shape: Benter's measure.
    A book this flat has no view of its own for the band to be widened to."""
    p = np.asarray(probs, dtype=float)
    p = p[p > 0]
    return float(np.clip(1 + (p * np.log(p)).sum() / math.log(len(probs)), 0.0, 1.0))


def cells(edges_a: np.ndarray, band: tuple[float, float]) -> list[tuple[float, float, int | None]]:
    """The band cut into A's ranges: (low, high, A range index), with a None index where the band runs past A's ladder
    and only B can cover."""
    L, U = band
    out: list[tuple[float, float, int | None]] = []
    if L < edges_a[0]:
        out.append((L, min(U, float(edges_a[0])), None))
    for j in range(len(edges_a) - 1):
        lo, hi = max(float(edges_a[j]), L), min(float(edges_a[j + 1]), U)
        if lo < hi:
            out.append((lo, hi, j))
    if U > edges_a[-1]:
        out.append((max(L, float(edges_a[-1])), U, None))
    return out


def reach(link: Link, lo: float, hi: float, edges_b: np.ndarray) -> tuple[int, int] | None:
    """The B ranges, first to last, that an A price in [lo, hi] can settle in; None if some of them lie off B's ladder.
    A price exactly on an edge counts for both of its ranges, so the cover never depends on the server's tie rule."""
    blo, bhi = link.image(lo, hi)
    if blo < edges_b[0] * (1 - 1e-9) - 1e-9 or bhi > edges_b[-1] * (1 + 1e-9) + 1e-9:
        return None
    n = len(edges_b) - 1
    k1 = int(np.searchsorted(edges_b, blo, side="left")) - 1
    k2 = int(np.searchsorted(edges_b, bhi, side="right")) - 1
    k1, k2 = min(max(k1, 0), n - 1), min(max(k2, 0), n - 1)
    return k1, max(k2, k1)


def cover(prices_a, edges_a, prices_b, edges_b, link: Link, band: tuple[float, float]) -> Cover | None:
    """The cheapest cover of the A prices in `band` across the two books, at today's marginal prices. Each cell of the
    band is bought on A or handed to B, which then holds every range the cell can reach. Since the cells' images run
    one way along B, a dynamic programme over the highest B range bought so far is exact. None if some cell can be
    covered by neither ladder."""
    pa = np.asarray(prices_a, dtype=float)
    pb = np.asarray(prices_b, dtype=float)
    ea = np.asarray(edges_a, dtype=float)
    eb = np.asarray(edges_b, dtype=float)
    nb = len(pb)
    flip = link.kind == "inverse"                       # up the A ladder is down the B ladder: relabel B so reaches rise
    pbr = pb[::-1] if flip else pb
    cum = np.concatenate([[0.0], np.cumsum(pbr)])
    rows = []
    for lo, hi, j in cells(ea, band):
        r = reach(link, lo, hi, eb)
        if r is not None and flip:
            r = (nb - 1 - r[1], nb - 1 - r[0])
        if r is None and j is None:
            return None
        rows.append((j, r))
    if not rows:
        return None
    INF = float("inf")
    states = np.arange(nb + 1)                          # state s: B ranges up to s − 1 (relabelled) are bought
    dp = np.full(nb + 1, INF)
    dp[0] = 0.0
    trail: list[tuple[np.ndarray, np.ndarray]] = []
    for j, r in rows:
        new, src, via_a = np.full(nb + 1, INF), np.full(nb + 1, -1), np.zeros(nb + 1, dtype=bool)
        if j is not None:
            new[:] = dp + pa[j]
            src[:] = states
            via_a[:] = True
        if r is not None:
            k1, k2 = r
            t = k2 + 1                                  # every state at or below t lands on t, paying for the ranges it lacks
            cand = dp[: t + 1] + cum[t] - cum[np.minimum(np.maximum(k1, states[: t + 1]), t)]
            i = int(np.argmin(cand))
            if cand[i] < new[t]:
                new[t], src[t], via_a[t] = cand[i], i, False
            stay = dp[t + 1:] < new[t + 1:]             # states already holding every range it needs pay nothing
            new[t + 1:][stay], src[t + 1:][stay], via_a[t + 1:][stay] = dp[t + 1:][stay], states[t + 1:][stay], False
        dp = new
        trail.append((src, via_a))
    end = int(np.argmin(dp))
    if not np.isfinite(dp[end]):
        return None
    sa, sb, state = [], set(), end
    for (j, r), (src, via_a) in zip(reversed(rows), reversed(trail), strict=True):
        if via_a[state]:
            sa.append(j)
        else:
            k1, k2 = r
            sb.update(range(max(k1, int(src[state])), k2 + 1))
        state = int(src[state])
    sb_idx = sorted((nb - 1 - k) if flip else k for k in sb)
    return Cover(band, tuple(sorted(sa)), tuple(sb_idx), float(dp[end]))


def charge(book: Book, idx, contracts: float) -> float:
    """Fee-inclusive sats to buy `contracts` on each of the ranges `idx` of one book, as one order: the LS-LMSR cost
    difference plus the commission, at least one sat of it."""
    if not idx or contracts <= 0:
        return 0.0
    q = list(book.shares)
    q2 = q[:]
    for i in idx:
        q2[i] += contracts
    raw = P.cost(q2, book.alpha) - P.cost(q, book.alpha)
    return raw + max(P.FEE * raw, P.MIN_FEE_SATS)


def price(book_a: Book, book_b: Book, cov: Cover, contracts: float) -> float:
    """What `contracts` of the cover cost, both legs, both fees."""
    return charge(book_a, cov.a, contracts) + charge(book_b, cov.b, contracts)


def size(book_a: Book, book_b: Book, cov: Cover, room: float, min_edge: float) -> tuple[float, float]:
    """(contracts, cost): the most contracts of the cover that cost at most `room` and still pay, at 98 a contract,
    `min_edge` more than they cost. The sizes that qualify form one window: below it the 1 sat minimum commission on
    each leg eats the return, above it buying has lifted both legs' prices too far. The search steps up into the window,
    doubles while it holds, and bisects to its top. (0, 0) if no size within the room qualifies."""
    def ok(d: float) -> bool:
        c = price(book_a, book_b, cov, d)
        return 0 < c <= room and PL.NET * d >= (1 + min_edge) * c

    if room < 1:
        return 0.0, 0.0
    lo = 0.01
    while not ok(lo):
        if lo > 1e6 or price(book_a, book_b, cov, lo) > room:       # cost only grows with size: nothing within the room
            return 0.0, 0.0
        lo *= 1.5
    hi = lo * 2
    while hi < 1e6 and ok(hi):
        lo, hi = hi, hi * 2
    for _ in range(40):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if ok(mid) else (lo, mid)
    d = math.floor(lo * 100) / 100
    return (d, price(book_a, book_b, cov, d)) if d >= 0.01 else (0.0, 0.0)


def bands(probs_a, edges_a, edges_b, link: Link, book_a: Book, book_b: Book, max_tail: float
          ) -> list[tuple[tuple[float, float], float]]:
    """The bands to try, widest first, each with the consensus mass it leaves outside: the whole price line either
    ladder can cover for certain, then the consensus's narrowest bands holding all but each of TAILS up to `max_tail`,
    each widened to hold both markets' own bands at that level (the subsidy floor taken off), so the cover is never
    narrower than what either book itself thinks possible."""
    p = np.asarray(probs_a, dtype=float)
    ea = np.asarray(edges_a, dtype=float)
    eb = np.asarray(edges_b, dtype=float)
    lo_b, hi_b = link.inside(float(eb[0]), float(eb[-1]))
    full = (min(float(ea[0]), lo_b), max(float(ea[-1]), hi_b))
    out: list[tuple[tuple[float, float], float]] = [(full, float(p[(ea[1:] <= full[0]) | (ea[:-1] >= full[1])].sum()))]
    own_a = P.signal(book_a.probs) if formed(book_a.probs) >= MIN_FORMED else None
    own_b = P.signal(book_b.probs) if formed(book_b.probs) >= MIN_FORMED else None
    for tail in sorted({t for t in TAILS if t <= max_tail} | {max_tail}):
        i, j = P.hdi(list(p), 1 - tail)
        L, U = float(ea[i]), float(ea[j + 1])
        if own_a is not None:
            i, j = P.hdi(own_a, 1 - tail)
            L, U = min(L, float(ea[i])), max(U, float(ea[j + 1]))
        if own_b is not None:
            i, j = P.hdi(own_b, 1 - tail)
            blo, bhi = link.back(float(eb[i]), float(eb[j + 1]))
            L, U = min(L, blo), max(U, bhi)
        L, U = max(L, full[0]), min(U, full[1])
        if (L, U) != out[-1][0] and L < U:
            out.append(((L, U), float(p[(ea[1:] <= L) | (ea[:-1] >= U)].sum())))
    return out


def best(book_a: Book, book_b: Book, probs_a, link: Link, min_edge: float, max_tail: float) -> tuple[Cover | None, Cover | None]:
    """(the cover to take, the best cover seen): the widest band whose cover returns `min_edge` at the margin, and the
    highest-returning cover of any band for the log when none does."""
    ea = np.array([book_a.bins[0][0]] + [hi for _, hi in book_a.bins], dtype=float)
    eb = np.array([book_b.bins[0][0]] + [hi for _, hi in book_b.bins], dtype=float)
    seen: Cover | None = None
    for band, tail in bands(probs_a, ea, eb, link, book_a, book_b, max_tail):
        c = cover(book_a.prices, ea, book_b.prices, eb, link, band)
        if c is None:
            continue
        c = Cover(c.band, c.a, c.b, c.marginal, tail)
        if seen is None or c.gross > seen.gross:
            seen = c
        if c.gross >= min_edge:
            return c, seen
    return None, seen


# ── the runner ──────────────────────────────────────────────

@dataclass(frozen=True)
class Pair:
    """Two closes at one instant, in two series, as the bot trades them: A the ladder the consensus is drawn on."""
    end: int
    a: MarketRow
    b: MarketRow
    label_a: str
    label_b: str
    kind: str                       # Link.kind
    rank: int = 0                   # the order of the pairs at one instant: the rule's place in its scheme

    @property
    def name(self) -> str:
        return f"{self.label_a}·{self.label_b}"


@dataclass
class Arbitrageur(Runner):
    """One bot across a scheme's series. Each visit takes the two books of one pair of coinciding closes, nearest first
    and the nearest again every other visit, finds the cheapest cover of the price line across them, and buys it when
    it returns `min_edge` on its cost whichever way the price goes. Covers are held to the close. Build it with
    `batch_id=""` and `feed=None`: it keeps a feed per asset (`make_feed`). The scheme is the bot's (`scheme_of`)
    unless given."""
    rules: Rules = field(default_factory=Rules.load)
    scheme: Scheme | None = None
    make_feed: Callable[[str], object] | None = None
    batches: list[Batch] = field(default_factory=list)
    feeds: dict[str, object] = field(default_factory=dict)
    _pairs: list[Pair] = field(default_factory=list)
    _where: dict[int, tuple[str, str]] = field(default_factory=dict)    # topic -> (asset, series label)
    _assets: dict[str, str] = field(default_factory=dict)               # series label -> asset with the price feed
    _pcursor: tuple[int, int] = (0, -1)
    _turn: int = 0
    _sigma: dict[tuple[str, str], tuple[object, float]] = field(default_factory=dict)
    _quiet: tuple[str, float] = ("", 0.0)

    def __post_init__(self) -> None:
        super().__post_init__()
        self.scheme = self.scheme or scheme_of(self.bot)
        self.series = " · ".join(self.scheme.series)

    def _asset_of(self, topic_id: int) -> str:
        return self._where.get(topic_id, ("", ""))[0]

    def _label_of(self, topic_id: int) -> str:
        return self._where.get(topic_id, ("", ""))[1]

    def _intro(self) -> str:
        return (f"budget {fmt.sats(self.cfg.bankroll_sats)} across {self.series}  ·  every {self.cfg.interval_s:g}s one pair of closes "
                f"at the same instant ({self.scheme.pairs_text}), the nearest every other visit, the rest nearest to farthest  ·  "
                f"buys the two books' cheapest "
                f"cover of the price line when it returns {self.cfg.min_edge:.0%} on cost whichever way the price goes, and holds "
                f"it to the close  ·  at most {self.rules.pair_share:.0%} of the budget in one pair, "
                f"{self.rules.max_tail:.2%} of the consensus left outside the cover")

    def _feed(self, asset: str):
        if asset not in self.feeds:
            if self.make_feed is None:
                from .zoo.data import Feed

                self.feeds[asset] = Feed(asset)
            else:
                self.feeds[asset] = self.make_feed(asset)
        return self.feeds[asset]

    def _idle(self, msg: str) -> None:
        if msg != self._quiet[0] or time.time() - self._quiet[1] >= 60:
            self.say(msg)
            self._quiet = (msg, time.time())

    def tag(self, pair: Pair) -> str:  # type: ignore[override]
        return (f"{self.step}/{self.of}  " if self.of else "") + f"{pair.name} at {fmt.question('', pair.end)[4:-4]}"

    async def _rows(self) -> dict[str, list[MarketRow]]:
        """Every open close of the scheme's series, by series label, each list read again every `list_s`."""
        if not self.batches:
            self.batches = [b for b in await self.api.batches() if b.short in self.scheme.series]
        got = await asyncio.gather(*(listing(self.api, b.batch_id, self.cfg.list_s) for b in self.batches))
        out: dict[str, list[MarketRow]] = {}
        for b, rs in zip(self.batches, got, strict=True):
            self._assets[b.short] = b.asset
            for r in rs:
                self._where[r.topic_id] = (b.asset, b.short)
            out[b.short] = rs
        return out

    def _pair_up(self, rows: dict[str, list[MarketRow]], now: float) -> list[Pair]:
        """Every pair of closes at one instant the scheme's rules allow, nearest instant first and in the rules' order
        within one instant."""
        def live(label: str) -> dict[int, MarketRow]:
            return {r.end_time_utc: r for r in rows.get(label, [])
                    if r.quote_mode == "live" and r.end_time_utc - now >= self.cfg.stop_before_close_s}

        by = {label: live(label) for label in self.scheme.series}
        out = []
        for rank, rule in enumerate(self.scheme.rules):
            a, b = by[rule.a], by[rule.b]
            out.extend(Pair(end, a[end], b[end], rule.a, rule.b, rule.kind, rank) for end in a.keys() & b.keys())
        return sorted(out, key=lambda p: (p.end, p.rank))

    def _next_pair(self) -> Pair:
        """The nearest pair every other visit; in between, the pair after the one visited last, and round again."""
        self._turn += 1
        if self._turn % 2:
            pair = self._pairs[0]
        else:
            ahead = [p for p in self._pairs if (p.end, p.rank) > self._pcursor]
            if not ahead:
                self._finish_round()
                ahead = self._pairs
            pair = ahead[0]
            self._pcursor = (pair.end, pair.rank)
        self.step, self.of = self._pairs.index(pair) + 1, len(self._pairs)
        return pair

    async def cycle(self) -> None:
        self.cycles += 1
        now = time.time()
        self.ledger.prune(now)
        try:
            rows = await self._rows()
        except ApiError as e:
            self.say(f"cannot read the series' closes: {e}")
            return
        self._series = {r.topic_id for rs in rows.values() for r in rs}
        self._pairs = self._pair_up(rows, now)
        if not self._pairs:
            self._idle("no two closes at one instant are open to trading")
            return
        self._quiet = ("", 0.0)
        pair = self._next_pair()
        fa, fb = self._feeds_of(pair)
        self.feed = fa
        await asyncio.gather(*(f.refresh(max_age=max(self.cfg.interval_s, 1.0)) for f in {id(fa): fa, id(fb): fb}.values()))
        if not fa.ready or not fb.ready:
            self.say(f"{self.tag(pair)}  no price data yet, skipped")
            return
        await self._at_pair(pair)

    def _feeds_of(self, pair: Pair):
        """The two sides' price feeds: one object for two markets on one asset."""
        return self._feed(self._assets.get(pair.label_a, pair.label_a)), self._feed(self._assets.get(pair.label_b, pair.label_b))

    def _link(self, pair: Pair, hours: float) -> Link:
        """How B follows A for this pair now: k from the two spots (B × A inverse, B / A direct, 1 for one price), the
        slack from the free leg's measured hourly σ over the hours to the close."""
        if pair.kind == "same":
            return Link("same", 1.0, self.rules.same_band)
        fa, fb = self._feeds_of(pair)
        key = (fa.bars.index[-1], fb.bars.index[-1])
        kept = self._sigma.get((pair.label_a, pair.label_b))
        if kept is None or kept[0] != key:
            kept = self._sigma[(pair.label_a, pair.label_b)] = (key, link_sigma(fa.bars, fb.bars, pair.kind))
        k = float(fb.spot) * float(fa.spot) if pair.kind == "inverse" else float(fb.spot) / float(fa.spot) if fa.spot else 0.0
        return Link(pair.kind, k, slack(self.rules, pair.kind, kept[1], hours))

    async def _at_pair(self, pair: Pair) -> None:
        """The visit: both books read now, the consensus drawn on the Bitcoin ladder, the cheapest cover found, sized
        against the budget and bought in one request."""
        try:
            got = await asyncio.gather(self.api.book(pair.a.topic_id), self.api.book(pair.b.topic_id))
        except ApiError as e:
            self.say(f"{self.tag(pair)}  cannot read its books: {e}")
            return
        book_a, book_b = (self._paper_book(b) for b in got)
        now = time.time()
        for b in (book_a, book_b):
            if not b.bins or b.quote_mode != "live" or b.end_time_utc - now < self.cfg.stop_before_close_s:
                self.say(f"{self.tag(pair)}  closed to trading, skipped")
                return
            self._remember(b)
        hours = max((pair.end - now) / 3600.0, 1e-3)
        link = self._link(pair, hours)
        if not link.k > 0:
            self.say(f"{self.tag(pair)}  no spot for the {pair.label_b} leg yet, skipped")
            return
        feed = self._feeds_of(pair)[0]
        try:
            probs = await off_loop(self._draw, feed, book_a, ())
            take, seen = await off_loop(best, book_a, book_b, probs, link, self.cfg.min_edge, self.rules.max_tail)
        except Exception as e:
            self.say(f"{self.tag(pair)}  no picture: {e}")
            return
        if self._stopping:
            return
        self._mark(Picture(book_a, list(book_a.probs), 0.0))
        self._mark(Picture(book_b, list(book_b.probs), 0.0))
        self._round["visited"] += 1
        band = self._slack_text(pair, link)
        if seen is None:
            self.say(f"{self.tag(pair)}  no band of the price line both ladders can cover ({band})")
            return
        where = f"covers {fmt.kprice(seen.band[0])}–{fmt.kprice(seen.band[1])}, {fmt.pct(seen.tail)} of the consensus outside"
        if take is None:
            self.say(f"{self.tag(pair)}  best cover {seen.marginal:.1f} sats a contract for {PL.NET:.0f} back, {fmt.roi(seen.gross)} "
                     f"after fees; needs {fmt.roi(self.cfg.min_edge)}  ·  {where}  ·  {band}")
            return
        room, why = self._pair_room(pair)
        if why:
            self.say(f"{self.tag(pair)}  cover at {fmt.roi(take.gross)} but {why}")
            return
        d, cost = size(book_a, book_b, take, room, self.cfg.min_edge)
        if d < 0.01:
            self.say(f"{self.tag(pair)}  cover at {fmt.roi(take.gross)} at the margin, but no size holds {fmt.roi(self.cfg.min_edge)} "
                     f"within {fmt.sats(room)}  ·  {where}")
            return
        if await self._enter_pair(pair, book_a, book_b, take, d, cost, link) and self.live and self.on_trade:
            self.on_trade()
        self.ledger.save()

    @staticmethod
    def _slack_text(pair: Pair, link: Link) -> str:
        return f"{link.band:.1%} slack " + ("between the two settlements" if link.kind == "same" else f"on the {pair.label_b} leg")

    def _pair_room(self, pair: Pair) -> tuple[float, str]:
        """(sats this visit may spend on the pair, why nothing if nothing)."""
        held = sum(self.ledger.open_cost(self.key, self.mode, t) for t in (pair.a.topic_id, pair.b.topic_id))
        left = self.cfg.bankroll_sats - self.open_cost
        per = min(self.rules.pair_share * self.cfg.bankroll_sats, self.cfg.max_per_cycle_sats)
        hour = self.cfg.max_per_hour_sats - self.spent_last_hour()
        room = min(per - held, left, hour)
        if room >= 1:
            return room, ""
        if left < 1:
            return room, f"budget fully deployed: {fmt.sats(self.open_cost)} held, waiting for closes to settle"
        if per - held < 1:
            return room, f"holds {fmt.sats(held)} in this pair, its full share of {fmt.sats(per)}"
        return room, f"hourly cap of {fmt.sats(self.cfg.max_per_hour_sats)} reached"

    @staticmethod
    def _legs(book: Book, idx, contracts: float) -> list[tuple[int, float]]:
        return [(book.option_ids[i], contracts) for i in idx]

    def _describe(self, book: Book, idx, label: str) -> str:
        if not idx:
            return f"nothing on {label}"
        lo, hi = book.bins[idx[0]][0], book.bins[idx[-1]][1]
        gaps = sum(1 for x, y in zip(idx, idx[1:], strict=False) if y != x + 1)
        holes = f" with {gaps} gap{'s' if gaps > 1 else ''}" if gaps else ""
        return f"{label} {fmt.span(lo, hi)} in {len(idx)} range{'' if len(idx) == 1 else 's'}{holes}"

    async def _enter_pair(self, pair: Pair, book_a: Book, book_b: Book, cov: Cover, d: float, cost: float, link: Link) -> bool:
        """Both legs in one request. Live, each leg is checked against the server's estimate first. If the server fills
        one leg and refuses the other, the filled leg is sold straight back: an unhedged leg is a bet on direction,
        which this bot never holds on purpose."""
        legs_a, legs_b = self._legs(book_a, cov.a, d), self._legs(book_b, cov.b, d)
        local_a, local_b = charge(book_a, cov.a, d), charge(book_b, cov.b, d)
        pays = PL.NET * d
        if self.live:
            sides = [(b.topic_id, legs) for b, legs in ((book_a, legs_a), (book_b, legs_b)) if legs]
            try:
                est = await asyncio.gather(*(self.api.estimate_legs(t, legs) for t, legs in sides))
            except ApiError as e:
                self.say(f"{self.tag(pair)}  no estimate: {e}")
                return False
            server = sum(raw + max(fee, P.MIN_FEE_SATS) for raw, fee in est)
            if abs(server - cost) > self.cfg.estimate_tolerance * cost + 5 or pays < (1 + self.cfg.min_edge) * server:
                self.say(f"{self.tag(pair)}  book moved (local {cost:,.0f} vs server {server:,.0f}), skipped")
                return False
        self.say(f"{self.tag(pair)}  {'BUY' if self.live else 'would buy'} cover of {fmt.contracts(d)} contracts: "
                 f"{self._describe(book_a, cov.a, pair.label_a)} + {self._describe(book_b, cov.b, pair.label_b)}  "
                 f"for {fmt.sats(cost)}, pays {fmt.sats(pays)} wherever {fmt.kprice(cov.band[0])}–{fmt.kprice(cov.band[1])} it closes "
                 f"({fmt.sats(pays - cost, signed=True)}, {fmt.roi(pays / cost - 1)})  ·  {fmt.pct(cov.tail)} of the consensus outside  ·  "
                 f"{self._slack_text(pair, link)}")
        charges = {book_a.topic_id: local_a, book_b.topic_id: local_b}
        if self.live:
            groups = [(b.topic_id, legs) for b, legs in ((book_a, legs_a), (book_b, legs_b)) if legs]
            try:
                fills = await self.api.buy_many(groups)
            except ApiError as e:
                self.say(f"{self.tag(pair)}  buy failed: {e}")
                if "may or may not" in str(e):
                    self.stop()
                return False
            bad = [f for f in fills if f.error]
            if bad:
                good = [(f, dict(groups)[f.topic_id]) for f in fills if not f.error]
                self.say(f"{self.tag(pair)}  one leg rejected: {bad[0].error}" + ("; selling the other back" if good else ""))
                for f, legs in good:
                    await self._unwind(pair, f.topic_id, legs, f.cost_sats + f.fee_sats, book_a if f.topic_id == book_a.topic_id else book_b)
                return False
            charges = {f.topic_id: f.cost_sats + f.fee_sats for f in fills}
            cost = sum(charges.values())
        for book, idx, label in ((book_a, cov.a, pair.label_a), (book_b, cov.b, pair.label_b)):
            if not idx:
                continue
            weights = np.asarray([book.prices[i] for i in idx], dtype=float)
            weights = weights / weights.sum() if weights.sum() > 0 else np.full(len(idx), 1 / len(idx))
            for i, w in zip(idx, weights, strict=True):
                self.ledger.add(self.key, self.mode, book, label, i, d, charges[book.topic_id] * float(w))
            moved = _lifted(book, idx, d)
            self._mark(Picture(moved, list(moved.probs), 0.0))
            self._remember(moved)
        self.spent.append((time.time(), cost))
        self.paid_sats += cost
        self.trades += 1
        self._round["bought"] += 1
        self._round["paid"] += cost
        return True

    async def _unwind(self, pair: Pair, topic_id: int, legs: list[tuple[int, float]], paid: float, book: Book) -> None:
        """Sell a leg the server filled on its own. If that fails too, the leg goes into the ledger so it is seen."""
        label = self._label_of(topic_id) or "the leg"
        try:
            fills = await self.api.sell_many([(topic_id, legs)])
        except ApiError as e:
            fills, err = [], str(e)
        else:
            err = fills[0].error if fills else "no result returned"
        if fills and not err:
            got = fills[0].cost_sats
            self.sold_sats += got
            self.gain_sats += got - paid
            self._round["sold_on"] += 1
            self._round["sold"] += got
            self._round["gain"] += got - paid
            self.say(f"{self.tag(pair)}  sold {label} back for {fmt.sats(got)} ({fmt.sats(got - paid, signed=True)} on cost)")
            return
        at = {o: i for i, o in enumerate(book.option_ids)}
        weights = np.asarray([book.prices[at[o]] for o, _ in legs], dtype=float)
        weights = weights / weights.sum() if weights.sum() > 0 else np.full(len(legs), 1 / len(legs))
        for (o, c), w in zip(legs, weights, strict=True):
            self.ledger.add(self.key, self.mode, book, label, at[o], c, paid * float(w))
        self.paid_sats += paid
        self.say(f"{self.tag(pair)}  could not sell {label} back ({err}): holding an UNHEDGED leg of {fmt.sats(paid)}, "
                 "a bet on direction this bot did not mean to make. Sell it yourself or run the Profit Taker.")
        if "may or may not" in err:
            self.stop()


def _lifted(book: Book, idx, contracts: float) -> Book:
    """The book once `contracts` are bought on each of `idx`: what the site will show next."""
    b = Book(book.topic_id, book.title, book.end_time_utc, book.quote_mode, book.volume_msat, list(book.option_ids),
             list(book.bins), list(book.shares), book.alpha)
    for i in idx:
        b.shares[i] += contracts
    b.reprice()
    return b


def rules_text(r: Rules | None = None, scheme: Scheme = BITCOIN) -> str:
    """The about screen's RUNNER section for an Arbitrageur of `scheme`."""
    r = r or Rules()
    return (
        f"It never backs a direction. Every few seconds it takes two closes at the same instant in two series ({scheme.pairs_text}), "
        "reads both books fresh, and finds the cheapest set of ranges across the two that holds the settlement price wherever it "
        "lands in a band: each range of one ladder is either bought there or every range of the other it can settle in is bought "
        f"instead, {scheme.free_text} allowed to move {r.dollar_z:g} hourly σ of its measured move (√h scaled) plus "
        f"{r.band_floor:.1%}, two markets on one price allowed {r.same_band:.1%} between their settlements. It tries the whole "
        "price line first (a true "
        "arbitrage, when it is there), then the widest band that still pays, so long as the zoo's consensus leaves at most "
        f"{r.max_tail:.2%} outside it. A contract of the cover pays 98 sats net in every scenario inside the band; the bot buys "
        "the largest size whose fee-inclusive cost, priced with the LS-LMSR cost function itself, still returns min_edge on "
        f"cost, never more than {r.pair_share:.0%} of the budget in one pair, both legs in one request, each checked against the "
        "server's estimate first. If the server fills one leg and refuses the other, the filled leg is sold straight back. "
        "Covers are held to the close and never sold. The log says on every visit what the best cover costs, what it pays "
        "and what the consensus leaves outside. Settings live in a [arbitrage] table of ~/.config/glimpse/bots.toml."
    )

