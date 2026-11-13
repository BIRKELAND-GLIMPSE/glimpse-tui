"""The Triangulator: one forecast, bet on the likely outcomes across every market that sells them cheap.

Three of Glimpse's hourly series close at the same second and are bound by an identity. An ounce of Tether Gold in
satoshis (`XAUTBTC`) is an ounce of gold in dollars (`XAU`) times 10⁸ over Bitcoin in dollars (`BTC`), give or take
the small basis between Tether Gold and PAX Gold, which the three spots show. So the three books at one instant are
three views of two unknowns, and one forecast of Bitcoin and gold is a forecast of all three.

The forecast is the lens: the Session Walk by default, a Student-t random walk whose σ adds up only the hours the
asset's market trades (gold and oil keep CME's week, so through a weekend their closes barely move), or any smoothing
or Benter model from the zoo. For each leg the bot draws the lens's picture of its close, then the picture the other
two legs imply, by Monte Carlo through a Gaussian copula whose correlation is read from the two feeds' hourly candles
(`implied`), and pools the two in log space (`pooled`), the implied picture weighing more the more the other two books
have formed and the less this one has. The three pooled pictures are the triangle: one consistent view of the instant.

Then it bets on the likely outcomes. On each leg the band is the narrowest run of ranges holding 80% of the triangle's
picture (`band`), and the bet is the same number of contracts on every range of it, so it pays 98 sats a set wherever
inside the band the price closes and nothing outside. The bot buys that set in every book that sells it for less than
it pays, sized by fractional Kelly on the band as one bet and filled only while the last set still returns `min_edge`
over what it costs (`band_fill`): buying lifts the price, and the bot stops well short of its own fair value instead
of paying it. All legs go in one request. First, though, the risk-free part: when a pair of the three books offers a
cover that pays more than it costs whichever way the price goes (`arb.best`), it buys that; and on every visit it
sells what the market now pays more for than the triangle says a position is worth, at a profit.

Measured on 31 October 2026, the books within a few hours of a close are already sharper than a calibrated walk, so
there the band costs more than it pays and the bot buys nothing; a day or two out the books still sit near their flat
seed and sell the band for 27 to 36 sats a set, and gold's weekend closes for less. That is where the bot buys, in
small stakes, since the books are thin and a few thousand sats move them. Every visit is one instant: the nearest
every other time, the rest within `max_hours` walked in between.

The Oil Triangulator is the same bot on a triangle with one leg missing. A barrel of WTI in satoshis (`WTIBTC`) is WTI
in dollars × 10⁸ over Bitcoin in dollars, but Glimpse has no WTI-in-dollars market, so that leg is synthetic: oil's
dollar price is read from the two feeds that carry it (WTI/BTC in sats × Bitcoin in dollars), its hourly move is
measured on the hours oil trades and the hours it does not (`Clock`, CME's week), and the leg's picture is a Student-t
at the close with the variance those hours add up to. At weekends oil stands still, the link between the two books is
tightest, and WTI/BTC is Bitcoin alone.
"""
from __future__ import annotations

import asyncio
import math
import time
import tomllib
from dataclasses import dataclass, field, fields

import numpy as np

from . import arb, fmt
from . import policy as PL
from . import pricing as P
from .api import ApiError, Book, MarketRow, parse_bin
from .arb import Arbitrageur, Pair
from .auth import config_dir
from .bots import Bot, Picture, _after, exits, off_loop, shown_series
from .clock import Clock, cme_open
from .policy import Leg

SATS = 1e8
EPS = 0.02                      # formedness the pooling weight assumes of a book that has none: two flat books pool evenly
FLOOR = 1e-9
MIN_SHARED = 200                # shared hourly bars before a correlation is read from the candles
LENS_FAMILIES = ("Baseline", "Smoothing", "Benter")   # the zoo families a lens is picked from (m on the advanced screen)


@dataclass(frozen=True)
class Rules:
    """How the Triangulator trades, read from a `[triangle]` table in ~/.config/glimpse/bots.toml."""
    lens: str = "session_walk"      # the zoo model whose picture of each close the triangle starts from
    cross_weight: float = 1.0       # most weight the implied picture can take against a leg's own (0: legs never meet)
    band_mass: float = 0.80         # the likely band: the narrowest run of ranges holding this much of the triangle's picture
    max_hours: float = 48.0         # instants further out are left alone: the picture is too wide and the sats sit too long
    instant_share: float = 0.10     # most of the budget held across the three closes of one instant
    covers: bool = True             # take a risk-free cover across two of the books when one is there
    cover_edge: float = 0.05        # what a cover must return on cost whichever way the price goes
    samples: int = 20_000           # Monte Carlo draws behind each implied picture
    smooth: float = 1.0             # ranges of Gaussian smoothing on an implied picture, to take the sampling noise off

    @classmethod
    def load(cls, **override) -> Rules:
        f = config_dir() / "bots.toml"
        raw = tomllib.loads(f.read_text()).get("triangle", {}) if f.is_file() else {}
        known = {x.name for x in fields(cls)}
        return cls(**{k: v for k, v in {**raw, **override}.items() if k in known})


@dataclass(frozen=True)
class Triangle:
    """Three legs bound by one identity: `cross` (an asset in satoshis) = `quote` (the asset in dollars) × 10⁸ /
    `base` (Bitcoin in dollars) × a basis between the two gold tokens, read from the three spots. `id` is the
    `arb.SCHEMES` scheme the bot trades (its market legs and the pairs a cover may take). A leg named in `synthetic`
    has no market on Glimpse: its picture is a model on the price the other two feeds imply (the oil leg), and only
    the other two are traded."""
    id: str = "triangle"
    base: str = "BTC"
    quote: str = "XAU"
    cross: str = "XAUTBTC"
    synthetic: str = ""
    name: str = "gold in satoshis"

    @property
    def legs(self) -> tuple[str, str, str]:
        return self.base, self.quote, self.cross

    @property
    def series(self) -> tuple[str, ...]:
        """The legs with a market: what is read and traded."""
        return tuple(x for x in self.legs if x != self.synthetic)

    def others(self, label: str) -> tuple[str, str]:
        return tuple(x for x in self.legs if x != label)  # type: ignore[return-value]

    def terms(self, label: str) -> tuple[tuple[str, int], tuple[str, int]]:
        """ln `label` = ln constant + Σ sign · ln other: how the other two legs make this one."""
        if label == self.cross:
            return (self.quote, +1), (self.base, -1)
        if label == self.quote:
            return (self.cross, +1), (self.base, +1)
        return (self.quote, +1), (self.cross, -1)

    def constant(self, label: str, basis: float) -> float:
        c = basis * SATS
        return 1 / c if label == self.quote else c

    def basis(self, spots: dict[str, float]) -> float:
        """What the cross spot is over what the other two spots say it should be: Tether Gold under PAX Gold. One
        exactly when the quote leg is synthetic, since its spot is read from the other two."""
        b, q, c = (spots.get(x, 0.0) for x in self.legs)
        return c * b / (q * SATS) if b > 0 and q > 0 and c > 0 else 0.0

    def implied_price(self, label: str, prices: dict[str, float], basis: float) -> float:
        """The price of `label` the other two prices imply."""
        (a, sa), (b, sb) = self.terms(label)
        return self.constant(label, basis) * prices[a] ** sa * prices[b] ** sb


TRIANGLE = Triangle()
OIL = Triangle("oil", "BTC", "WTI", "WTIBTC", synthetic="WTI", name="oil in satoshis")
TRIANGLES = {t.id: t for t in (TRIANGLE, OIL)}


def triangle_of(bot) -> Triangle | None:
    """The triangle a zoo entry names in `Model.arbitrage`, or None for a bot that is not a Triangulator."""
    return TRIANGLES.get(str(getattr(bot, "arbitrage", "")))


# ── the oil leg: a price with no market ─────────────────────

oil_open = cme_open             # WTI trades on CME's week; the clock module keeps the calendar


class DerivedFeed:
    """Oil in dollars, from the two feeds that carry it: WTI/BTC in satoshis a barrel × Bitcoin in dollars / 10⁸, bar by
    bar and spot by spot. The shape of a `zoo.data.Feed` as far as the Triangulator reads one."""

    def __init__(self, asset: str, num, den) -> None:
        self.asset, self.num, self.den = asset, num, den

    @property
    def ready(self) -> bool:
        return bool(self.num.ready and self.den.ready)

    @property
    def spot(self) -> float:
        return float(self.num.spot) * float(self.den.spot) / SATS if self.ready else 0.0

    @property
    def bars(self):
        j = self.num.bars[["close"]].join(self.den.bars[["close"]], how="inner", lsuffix="_n", rsuffix="_d")
        out = j[["close_n"]].rename(columns={"close_n": "close"})
        out["close"] = j["close_n"] * j["close_d"] / SATS
        return out

    async def refresh(self, max_age: float = 30.0) -> None:
        await asyncio.gather(self.num.refresh(max_age=max_age), self.den.refresh(max_age=max_age))


@dataclass(frozen=True)
class Synthetic:
    """A leg's picture as a model: a Student-t of the log price at the close around `spot`, with `sigma` from the clock."""
    spot: float
    sigma: float
    nu: float

    @property
    def unit(self) -> float:
        """What divides a t_ν draw to unit variance."""
        return math.sqrt(self.nu / (self.nu - 2)) if self.nu > 2 else 1.0

    def draw(self, u: np.ndarray) -> np.ndarray:
        """Log prices at cumulative probabilities `u`: the sampler `implied` uses in place of a ladder."""
        from scipy.stats import t as student

        return math.log(self.spot) + self.sigma * student.ppf(u, self.nu) / self.unit

    def ladder(self, n: int = 60, span: float = 4.0) -> list[tuple[float, float]]:
        """A ladder to draw the picture on: `n` log-spaced ranges `span` σ either side of spot."""
        e = self.spot * np.exp(np.linspace(-span, span, n + 1) * self.sigma)
        return [(float(lo), float(hi)) for lo, hi in zip(e[:-1], e[1:], strict=True)]

    def picture(self, bins) -> list[float]:
        """The model's probability of each range of `bins`, what falls past the ends in the end ranges."""
        from scipy.stats import t as student

        e = np.array([bins[0][0]] + [hi for _, hi in bins], dtype=float)
        c = student.cdf((np.log(e) - math.log(self.spot)) / self.sigma * self.unit, self.nu)
        c[0], c[-1] = 0.0, 1.0
        p = np.maximum(np.diff(c), FLOOR)
        return (p / p.sum()).tolist()


# ── the maths ───────────────────────────────────────────────

def log_edges(edges) -> np.ndarray:
    """A ladder's edges in log price. A first range that starts at 0 (the daily ladders) is given a tenth of its top
    as its bottom, so it has a finite log width."""
    e = np.asarray(edges, dtype=float).copy()
    if e[0] <= 0:
        e[0] = e[1] / 10
    return np.log(e)


def draw(probs, edges, u: np.ndarray) -> np.ndarray:
    """Log prices at cumulative probabilities `u` of a picture over a ladder: the inverse of its distribution function,
    log-uniform inside a range."""
    c = np.concatenate([[0.0], np.cumsum(np.asarray(probs, dtype=float))])
    c = c / c[-1] + np.arange(len(c)) * 1e-12                  # strictly increasing, so a zero range is no plateau
    return np.interp(u, c, log_edges(edges))


def correlation(bars_a, bars_b) -> float:
    """Correlation of the two feeds' hourly log returns over their last 1,000 shared hours; 0 until they share 200."""
    try:
        j = bars_a["close"].to_frame("a").join(bars_b["close"].to_frame("b"), how="inner")
        r = np.diff(np.log(j.to_numpy(dtype=float)), axis=0)
        r = r[np.isfinite(r).all(axis=1)][-1000:]
        if len(r) < MIN_SHARED:
            return 0.0
        with np.errstate(invalid="ignore", divide="ignore"):            # a leg that has not moved has no correlation
            c = float(np.corrcoef(r[:, 0], r[:, 1])[0, 1])
        return float(np.clip(c, -0.99, 0.99)) if math.isfinite(c) else 0.0
    except Exception:
        return 0.0


def smoothed(p: np.ndarray, width: float) -> np.ndarray:
    """A picture with a Gaussian of `width` ranges run over it, renormalised: the Monte Carlo grain taken off."""
    if width <= 0:
        return p
    r = max(int(math.ceil(3 * width)), 1)
    x = np.arange(-r, r + 1)
    k = np.exp(-0.5 * (x / width) ** 2)
    out = np.convolve(np.pad(p, r, mode="edge"), k / k.sum(), mode="valid")
    return out / out.sum()


def implied(edges, inputs: list[tuple], constant: float, rho: float,
            rng: np.random.Generator, samples: int = 20_000, smooth: float = 1.0) -> list[float]:
    """The picture of one leg the other two imply, over `edges`. `inputs` are (picture, edges, sign) for the two legs
    with ln leg = ln constant + Σ sign · ln input, or (Synthetic, None, sign) for a leg that is a model. The two are
    joined by a Gaussian copula of correlation `rho`: standard normal pairs become uniforms, the uniforms become prices
    through each picture's inverse distribution, and the identity gives a price of the leg, binned on its ladder. What
    lands past the ladder goes in its end ranges."""
    from scipy.special import ndtr

    z = rng.standard_normal((samples, 2))
    z[:, 1] = rho * z[:, 0] + math.sqrt(max(1 - rho * rho, 1e-12)) * z[:, 1]
    u = ndtr(z)
    lc = np.full(samples, math.log(constant))
    for (p, e, sign), col in zip(inputs, (0, 1), strict=True):
        lc += sign * (p.draw(u[:, col]) if e is None else draw(p, e, u[:, col]))
    le = log_edges(edges)
    idx = np.clip(np.searchsorted(le, lc, side="right") - 1, 0, len(le) - 2)
    h = np.bincount(idx, minlength=len(le) - 1).astype(float)
    out = np.maximum(smoothed(h / h.sum(), smooth), FLOOR)
    return (out / out.sum()).tolist()


def weight(own_formed: float, others_formed: float, cross_weight: float = 1.0) -> float:
    """How much the implied picture weighs against a leg's own: `cross_weight` × the other two books' formedness over
    both sides' together. A flat leg facing two formed books takes nearly the whole of `cross_weight`; a formed leg
    facing flat books takes almost none; equals pool at half of it."""
    return cross_weight * (others_formed + EPS) / (own_formed + others_formed + 2 * EPS)


def pooled(own, implied_, lam: float) -> list[float]:
    """The log-linear pool of a leg's own picture and the implied one at weight `lam`: own^(1/(1+λ)) · implied^(λ/(1+λ))."""
    lo = np.log(np.maximum(np.asarray(own, dtype=float), FLOOR))
    li = np.log(np.maximum(np.asarray(implied_, dtype=float), FLOOR))
    lw = (lo + lam * li) / (1 + lam)
    out = np.exp(lw - lw.max())
    return (out / out.sum()).tolist()


def median(bins, probs) -> float:
    return P.quantile(list(bins), list(probs), 0.5)


def market_picture(book: Book) -> list[float]:
    """The close as the site draws it: the LS-LMSR softmax of its shares, the common floor gone."""
    q = np.asarray(book.shares, dtype=float)
    return PL.shown(q, P.display_alpha(len(q))).tolist()


def pspan(lo: float, hi: float) -> str:
    """A range as the desk and the log print it: in thousands once prices run into millions (sats an ounce), to the
    cent on the oil model's ladder."""
    if hi >= 1_000_000:
        return f"{lo / 1000:,.0f}k–{hi / 1000:,.0f}k"
    if hi - lo < 1:
        return f"{lo:,.2f}–{hi:,.2f}"
    return fmt.span(lo, hi)


# ── the bet: the likely band ────────────────────────────────

def band(probs, mass: float) -> tuple[int, int]:
    """The likely outcomes: the narrowest run of ranges holding `mass` of a picture, as (first, last) range."""
    return P.hdi(list(probs), mass)


def _lifted(q, lo: int, hi: int, d: float) -> list[float]:
    q2 = list(q)
    for i in range(lo, hi + 1):
        q2[i] += d
    return q2


def set_price(q, alpha: float, lo: int, hi: int, d: float = 0.0) -> float:
    """What one contract on every range lo..hi costs at the margin, before the fee, once `d` such sets are bought."""
    return float(sum(P.prices(_lifted(q, lo, hi, d), alpha)[lo:hi + 1]))


def set_cost(q, alpha: float, lo: int, hi: int, d: float) -> float:
    """The fee-inclusive cost of `d` contracts on every range lo..hi."""
    return (P.cost(_lifted(q, lo, hi, d), alpha) - P.cost(list(q), alpha)) * (1 + P.FEE)


def pays(p: float, price: float) -> float:
    """What a set bought at `price` (before the fee) returns per sat in expectation: 98·p over what it costs with the fee."""
    return PL.NET * p / ((1 + P.FEE) * price) if price > 0 else 0.0


def band_stake(p: float, price: float, kelly: float, bankroll: float) -> float:
    """Fractional Kelly on the band as one bet: 98 a set with chance `p`, for `price` plus the fee a set."""
    s = price * (1 + P.FEE)
    if s <= 0:
        return 0.0
    b = PL.NET / s - 1
    if b <= 0:
        return 0.0
    return max((b * p - (1 - p)) / b, 0.0) * kelly * bankroll


def band_fill(book: Book, lo: int, hi: int, p: float, stake: float, min_edge: float) -> tuple[float, float]:
    """(contracts a range, fee-inclusive cost) of the largest equal-contract buy of ranges lo..hi within `stake` whose
    last set still returns `min_edge`: 98·p ≥ (1 + min_edge)(1 + fee) × the set's marginal price. Buying lifts the
    price, so the fill stops well short of the picture's fair value instead of paying it."""
    target = PL.NET * p / ((1 + P.FEE) * (1 + min_edge))
    q, a = book.shares, book.alpha
    p0 = set_price(q, a, lo, hi)
    if stake < 1 or p0 <= 0 or p0 > target:
        return 0.0, 0.0
    lo_d, hi_d = 0.0, stake / ((1 + P.FEE) * p0) + 0.01           # prices only rise: d sets cost at least d·p0 plus the fee
    for _ in range(40):
        mid = (lo_d + hi_d) / 2
        if set_price(q, a, lo, hi, mid) > target or set_cost(q, a, lo, hi, mid) > stake:
            hi_d = mid
        else:
            lo_d = mid
    d = math.floor(lo_d * 100) / 100
    if d < 0.01:
        return 0.0, 0.0
    return d, set_cost(q, a, lo, hi, d)


def band_legs(book: Book, lo: int, hi: int, d: float, cost: float) -> list[Leg]:
    """The set as the legs of one order, the cost shared out by each range's price halfway through the fill."""
    px = P.prices(_lifted(book.shares, lo, hi, d / 2), book.alpha)[lo:hi + 1]
    total = sum(px) or 1.0
    return [Leg(i, d, cost * x / total) for i, x in zip(range(lo, hi + 1), px, strict=True)]


# ── one visit, as the screen sees it ────────────────────────

@dataclass
class Side:
    """One leg of the triangle at one instant: its book, the pictures of it, and what the bot did there."""
    label: str
    book: Book
    spot: float
    formed: float                                   # how much of a shape the book's traders have given it (0 flat, 1 sharp)
    market: list[float] = field(default_factory=list)   # the close as the site draws it
    own: list[float] | None = None                  # the lens's picture of this close alone
    implied: list[float] | None = None              # the picture the other two legs imply
    final: list[float] | None = None                # the triangle: the two pooled
    weight: float = 0.0                             # the implied picture's weight in the pool
    buys: list[Leg] = field(default_factory=list)   # what it bought, or would buy, here on this visit
    sold: list[tuple[int, float, float]] = field(default_factory=list)   # (range, contracts, proceeds) sold here
    held: dict[int, tuple[float, float]] = field(default_factory=dict)   # range -> (contracts, cost) it holds here
    band: tuple[int, int] | None = None             # the likely band: the narrowest run of ranges holding band_mass of the triangle
    band_p: float = 0.0                             # the triangle's chance the close lands in the band
    band_price: float = 0.0                         # what one contract on each range of the band costs now, before the fee
    edge: float = 0.0                               # what a set of the band bought now returns per sat by the triangle, − 1
    synthetic: bool = False                         # a model, not a market: drawn on a ladder of its own, never traded
    note: str = ""                                  # a synthetic leg's account of itself: its σ to the close, its hours

    def medians(self) -> dict[str, float]:
        out = {"market": median(self.book.bins, self.market)}
        for k in ("own", "implied", "final"):
            p = getattr(self, k)
            if p is not None:
                out[k] = median(self.book.bins, p)
        return out


@dataclass
class Visit:
    """The triangle at one instant: three sides, how far apart they sit, and what the bot did about it."""
    end: int
    at: float
    sides: dict[str, Side]
    basis: float
    rho: dict[tuple[str, str], float] = field(default_factory=dict)
    gaps: dict[str, float] = field(default_factory=dict)   # market / own / final: the cross book's median over what the other two imply, − 1
    cover: str = ""                                 # the cover bought this visit, if any
    note: str = ""                                  # what it did, or why nothing

    def gap_of(self, which: str, triangle: Triangle = TRIANGLE) -> float:
        """The cross leg's median over the one the base and quote medians imply, − 1, by one kind of picture."""
        meds = {label: s.medians().get(which) for label, s in self.sides.items()}
        if any(v is None or v <= 0 for v in meds.values()) or self.basis <= 0:
            return 0.0
        return meds[triangle.cross] / triangle.implied_price(triangle.cross, meds, self.basis) - 1


# ── the runner ──────────────────────────────────────────────

@dataclass
class Triangulator(Arbitrageur):
    """One bot across the triangle's three series. Each visit takes the three closes at one instant, nearest first
    and the nearest again every other visit, draws the lens's picture of each, triangulates them into one consistent
    view, and trades every leg toward it in one request: a risk-free cover first when a pair of books offers one,
    then profit taken where the market overpays, then fractional Kelly on the ranges priced under the triangle.
    `watch=True` draws everything and trades nothing: the advanced screen's live view before the bot is run."""
    lens: Bot | None = None                         # the model the picture of each close starts from; None: the bot's own (the consensus)
    tri: Rules = field(default_factory=Rules.load)
    triangle: Triangle | None = None                # the bot's (`triangle_of`) unless given
    watch: bool = False
    focus_end: int | None = None                    # watching: the instant the screen wants drawn, if any
    last: Visit | None = None
    instants: list[int] = field(default_factory=list)
    _by: dict[str, dict[int, MarketRow]] = field(default_factory=dict)   # series label -> close time -> row
    _icursor: int = 0
    _corr: dict[tuple[str, str], tuple[object, float]] = field(default_factory=dict)
    _clock: tuple[object, Clock] | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        self.triangle = self.triangle or triangle_of(self.bot) or TRIANGLE
        self.scheme = arb.SCHEMES[self.triangle.id]
        self.series = " · ".join(self.triangle.series)

    @property
    def lens_name(self) -> str:
        return self.lens.name if self.lens is not None and self.lens is not self.bot else "the consensus"

    def _intro(self) -> str:
        return (f"budget {fmt.sats(self.cfg.bankroll_sats)} across {self.series}  ·  every {self.cfg.interval_s:g}s the closes at one "
                f"instant within {self.tri.max_hours:g} h, the nearest every other visit, the rest nearest to farthest  ·  lens "
                f"{self.lens_name}  ·  each leg's picture pooled with the one the other two imply ({self.triangle.cross} = "
                f"{self.triangle.quote} × 10⁸ / {self.triangle.base}"
                + (f", {self.triangle.synthetic} a model on the price the two feeds imply" if self.triangle.synthetic else "") + ")  ·  "
                f"{'a risk-free cover first when one pair of books offers it, then ' if self.tri.covers else ''}profit taken where the "
                f"market overpays, and the triangle's {self.tri.band_mass:.0%} band bought equal-contract in every book that sells it "
                f"{self.cfg.min_edge:.0%} under what it pays, every leg in one request  ·  at most "
                f"{self.cfg.edge_close_share:.0%} of the budget in one close and {self.tri.instant_share:.0%} in one instant")

    def tag(self, end) -> str:  # type: ignore[override]
        """`3/168  BTC·XAU·XAUTBTC at 30 Oct 15:00` for an instant; a pair's or one book's own name for a cover or a sale."""
        if isinstance(end, Pair):
            return super().tag(end)
        if isinstance(end, Book | MarketRow):
            return (f"{self.step}/{self.of}  " if self.of else "") + fmt.question(self._label_of(end.topic_id), end.end_time_utc)[:-4]
        return (f"{self.step}/{self.of}  " if self.of else "") + f"{'·'.join(self.triangle.legs)} at {fmt.question('', end)[4:-4]}"

    # the round ──────────────────────────────────────────────

    def _next_instant(self) -> int:
        """Watching, the instant the screen asks for. Else the nearest every other visit; in between, the one after the
        last visited, and round again."""
        if self.watch and self.focus_end in self.instants:
            end = self.focus_end
        else:
            self._turn += 1
            if self._turn % 2 or self.watch:
                end = self.instants[0]
            else:
                ahead = [e for e in self.instants if e > self._icursor]
                if not ahead:
                    self._finish_round()
                    ahead = self.instants
                end = ahead[0]
                self._icursor = end
        self.step, self.of = self.instants.index(end) + 1, len(self.instants)
        return end

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
        self._by = {label: {r.end_time_utc: r for r in rows.get(label, [])
                            if r.quote_mode == "live" and r.end_time_utc - now >= self.cfg.stop_before_close_s}
                    for label in self.triangle.series}
        common = set.intersection(*(set(d) for d in self._by.values())) if all(self._by.values()) else set()
        self.instants = sorted(e for e in common if e - now <= self.tri.max_hours * 3600)
        if not self.instants:
            missing = [label for label, d in self._by.items() if not d]
            self._idle(f"no instant within {self.tri.max_hours:g} h at which {'all three' if len(self._by) == 3 else 'both'} series are "
                       "open to trading" + (f" ({', '.join(missing)} not listed)" if missing else ""))
            return
        self._quiet = ("", 0.0)
        end = self._next_instant()
        feeds = {label: self._feed(self._assets.get(label, label)) for label in self.triangle.series}
        if self.triangle.synthetic:
            feeds[self.triangle.synthetic] = self._derived(feeds)
        self.feed = feeds[self.triangle.base]
        await asyncio.gather(*(f.refresh(max_age=max(self.cfg.interval_s, 1.0)) for f in {id(f): f for f in feeds.values()}.values()))
        if not all(f.ready for f in feeds.values()):
            self.say(f"{self.tag(end)}  no price data yet, skipped")
            return
        await self._at_instant(end, feeds)

    # one instant ────────────────────────────────────────────

    def _derived(self, feeds):
        """The synthetic leg's feed: the quote in dollars from the cross in sats × the base in dollars."""
        t = self.triangle
        if t.synthetic not in self.feeds:
            self.feeds[t.synthetic] = DerivedFeed(t.synthetic, feeds[t.cross], feeds[t.base])
        return self.feeds[t.synthetic]

    def _synthetic(self, feed, now: float, end: int) -> tuple[Synthetic, str]:
        """The synthetic leg's model at this instant: the clock fitted once per set of bars, the σ summed to the close."""
        bars = feed.bars
        key = (bars.index[-1], len(bars))
        if self._clock is None or self._clock[0] != key:
            self._clock = (key, Clock.fit(bars, "cme"))
        clock = self._clock[1]
        return Synthetic(float(feed.spot), clock.sigma_to(now, end), clock.nu), clock.describe(now, end)

    def _rho(self, a: str, b: str, feeds) -> float:
        key = (feeds[a].bars.index[-1], feeds[b].bars.index[-1])
        kept = self._corr.get((a, b))
        if kept is None or kept[0] != key:
            kept = self._corr[(a, b)] = (key, correlation(feeds[a].bars, feeds[b].bars))
        return kept[1]

    def _shown_for(self, label: str, book: Book) -> tuple:
        """The series as the site shows it, for a lens that reads the whole series: this close from its book, the rest
        from this runner's latest read of each or from the listing, paper fills laid over."""
        if self.lens is None or not self.lens.reads_series:
            return ()
        out = []
        for end, r in self._by.get(label, {}).items():
            if r.topic_id == book.topic_id:
                out.append((book.end_time_utc, book.bins, book.shares))
                continue
            _, q = self._shown.get(r.topic_id, (0.0, None))
            if q is None:
                q = list(r.shares)
                if not self.live:
                    held = {o: n for o, (n, _) in self.ledger.legs(self.key, self.mode, r.topic_id).items()}
                    index = {o: i for i, o in enumerate(list(r.option_ids) or range(1, len(q) + 1))}
                    for o, n in held.items():
                        if o in index:
                            q[index[o]] = max(q[index[o]] + n, 0.0)
            bins = book.bins if len(q) == len(book.bins) else [parse_bin(n) for n in r.names]
            out.append((end, bins, q))
        return shown_series(out)

    def _picture(self, end: int, books: dict[str, Book], feeds, spots: dict[str, float], basis: float) -> Visit:
        """In a runner thread: the lens's picture of each leg, the picture the other two imply, and their pool. A
        synthetic leg's picture is its model's, drawn on a ladder of its own, and it takes no pool."""
        lens = self.lens or self.bot
        now = time.time()
        sides: dict[str, Side] = {}
        edges: dict[str, np.ndarray | None] = {}
        models: dict[str, Synthetic] = {}
        for label in self.triangle.legs:
            if label in books:
                book = books[label]
                ctx = feeds[label].ctx(book.bins, book.end_time_utc)
                own = [float(x) for x in lens.probs(book, ctx, self._shown_for(label, book))]
                if len(own) != len(book.bins) or abs(sum(own) - 1) > 1e-6:
                    raise ValueError(f"{lens.name} must return {len(book.bins)} probabilities summing to 1 for {label}")
                sides[label] = Side(label, book, spots[label], arb.formed(book.probs), market_picture(book), own=own)
                edges[label] = np.array([book.bins[0][0]] + [hi for _, hi in book.bins], dtype=float)
                continue
            model, note = self._synthetic(feeds[label], now, end)
            bins = model.ladder()
            book = Book(-1, f"{label} in dollars (model)", end, "live", 0, list(range(1, len(bins) + 1)), bins, [1.0] * len(bins),
                        P.alpha_for(len(bins)))
            book.reprice()
            pic = model.picture(bins)
            sides[label] = Side(label, book, spots[label], 1.0, pic, own=pic, final=pic, synthetic=True, note=note)
            edges[label] = None
            models[label] = model
        rho: dict[tuple[str, str], float] = {}
        rng = np.random.default_rng(int(end))                   # one seed per instant: the pictures hold still between visits
        for label, side in sides.items():
            if side.synthetic:
                continue
            (a, sa), (b, sb) = self.triangle.terms(label)
            rho[(a, b)] = self._rho(a, b, feeds)
            inputs = [(models[x] if x in models else sides[x].own, edges[x], sx) for x, sx in ((a, sa), (b, sb))]
            side.implied = implied(edges[label], inputs, self.triangle.constant(label, basis), rho[(a, b)], rng,
                                   self.tri.samples, self.tri.smooth)
            side.weight = weight(side.formed, min(sides[a].formed, sides[b].formed), self.tri.cross_weight)
            side.final = pooled(side.own, side.implied, side.weight)
            side.band = band(side.final, self.tri.band_mass)
            side.band_p = float(sum(side.final[side.band[0]:side.band[1] + 1]))
            side.band_price = set_price(side.book.shares, side.book.alpha, *side.band)
            side.edge = pays(side.band_p, side.band_price) - 1
        v = Visit(end, time.time(), sides, basis, rho)
        v.gaps = {k: v.gap_of(k, self.triangle) for k in ("market", "own", "final")}
        return v

    def _held(self, book: Book) -> dict[int, tuple[float, float]]:
        at = {o: i for i, o in enumerate(book.option_ids)}
        return {at[o]: x for o, x in self.ledger.legs(self.key, self.mode, book.topic_id).items() if o in at}

    def _instant_room(self, books: dict[str, Book]) -> tuple[float, str]:
        """(sats this visit may spend across the instant's three closes, why nothing if nothing)."""
        held = sum(self.ledger.open_cost(self.key, self.mode, b.topic_id) for b in books.values())
        left = self.cfg.bankroll_sats - self.open_cost
        per = min(self.tri.instant_share * self.cfg.bankroll_sats, self.cfg.max_per_cycle_sats)
        hour = self.cfg.max_per_hour_sats - self.spent_last_hour()
        room = min(per - held, left, hour)
        if self.watch:
            return per, ""
        if room >= 1:
            return room, ""
        if left < 1:
            return room, f"budget fully deployed: {fmt.sats(self.open_cost)} held, waiting for closes to settle"
        if per - held < 1:
            return room, f"holds {fmt.sats(held)} in this instant, its full share of {fmt.sats(per)}"
        return room, f"hourly cap of {fmt.sats(self.cfg.max_per_hour_sats)} reached"

    def _pair_room(self, pair: Pair) -> tuple[float, str]:
        """A cover's room is the instant's."""
        return self._instant_room({pair.label_a: Book.from_row(pair.a), pair.label_b: Book.from_row(pair.b)})

    async def _at_instant(self, end: int, feeds) -> None:
        rows_at = {label: self._by[label][end] for label in self.triangle.series}
        try:
            got = await asyncio.gather(*(self.api.book(r.topic_id) for r in rows_at.values()))
        except ApiError as e:
            self.say(f"{self.tag(end)}  cannot read its books: {e}")
            return
        books = {label: self._paper_book(b) for label, b in zip(rows_at, got, strict=True)}
        now = time.time()
        for label, b in books.items():
            if not b.bins or b.quote_mode != "live" or b.end_time_utc - now < self.cfg.stop_before_close_s:
                self.say(f"{self.tag(end)}  {label} closed to trading, skipped")
                return
            self._remember(b)
        spots = {label: float(feeds[label].spot or 0.0) for label in self.triangle.legs}
        basis = self.triangle.basis(spots)
        if not basis > 0:
            self.say(f"{self.tag(end)}  no spot for {', '.join(k for k, v in spots.items() if not v > 0)} yet, skipped")
            return
        hours = max((end - now) / 3600.0, 1e-3)
        try:
            visit = await off_loop(self._picture, end, books, feeds, spots, basis)
        except Exception as e:
            self.say(f"{self.tag(end)}  no picture: {e}")
            return
        if self._stopping:
            return
        for side in self._traded(visit):
            self._mark(Picture(side.book, side.final, 0.0))
            side.held = self._held(side.book)
        self._round["visited"] += 1
        if self.watch:
            room, _ = self._instant_room(books)
            planned = self._plan(visit, room)
            visit.note = ("watching: what it would buy now, nothing sent" if planned else
                          f"watching: nothing to buy now, {self._why_nothing(visit)}")
            self.last = visit
            return
        traded = await self._sell(visit)
        if self.tri.covers and await self._cover(visit, rows_at, hours):
            traded = True
        traded = await self._bet(visit) or traded
        self.last = visit
        if traded and self.live and self.on_trade:
            self.on_trade()
        self.ledger.save()

    @staticmethod
    def _traded(v: Visit) -> list[Side]:
        """The sides with a market: what is marked, held and traded."""
        return [s for s in v.sides.values() if not s.synthetic]

    def _gap_text(self, v: Visit) -> str:
        t = self.triangle
        return (f"{t.cross} book {fmt.roi(v.gaps['market'])} from what {t.base} and {t.quote} imply "
                f"(lens {fmt.roi(v.gaps['own'])}, triangle {fmt.roi(v.gaps['final'])})  ·  formed "
                + " ".join(f"{s.label} {s.formed:.0%}" for s in self._traded(v))
                + "".join(f"  ·  {s.label} model {s.note.split(' · ')[0]}" for s in v.sides.values() if s.synthetic))

    async def _sell(self, v: Visit) -> bool:
        """Profit taken on every leg the market overpays, each leg in one request; a leg sold on is read again."""
        traded = False
        for side in self._traded(v):
            held = self.ledger.legs(self.key, self.mode, side.book.topic_id)
            if not held:
                continue
            sells = exits(side.book, side.final, held, self.cfg)
            if not sells or not await self._exit(side.book, sells):
                continue
            traded = True
            side.sold = sells
            try:
                side.book = self._paper_book(await self.api.book(side.book.topic_id))
            except ApiError as e:
                self.say(f"{self.tag(v.end)}  cannot read {side.label} again after the sale: {e}")
                continue
            self._remember(side.book)
            side.held = self._held(side.book)
        return traded

    async def _cover(self, v: Visit, rows_at: dict[str, MarketRow], hours: float) -> bool:
        """The risk-free part: the first pair of the three books whose cheapest cover returns min_edge either way."""
        for rank, rule in enumerate(self.scheme.rules):
            a, b = v.sides[rule.a], v.sides[rule.b]
            pair = Pair(v.end, rows_at[rule.a], rows_at[rule.b], rule.a, rule.b, rule.kind, rank)
            link = self._link(pair, hours)
            if not link.k > 0:
                continue
            try:
                take, _ = await off_loop(arb.best, a.book, b.book, a.final, link, self.tri.cover_edge, self.rules.max_tail)
            except Exception as e:
                self.say(f"{self.tag(v.end)}  no cover of {pair.name}: {e}")
                continue
            if take is None:
                continue
            room, why = self._instant_room({s.label: s.book for s in self._traded(v)})
            if why:
                v.note = f"cover of {pair.name} at {fmt.roi(take.gross)} but {why}"
                return False
            d, cost = arb.size(a.book, b.book, take, room, self.tri.cover_edge)
            if d < 0.01:
                continue
            if await self._enter_pair(pair, a.book, b.book, take, d, cost, link):
                v.cover = (f"cover of {pair.name}: {fmt.contracts(d)} contracts for {fmt.sats(cost)}, pays {fmt.sats(PL.NET * d)} "
                           f"wherever {fmt.kprice(take.band[0])}–{fmt.kprice(take.band[1])} it closes")
                for side in (a, b):
                    try:
                        side.book = self._paper_book(await self.api.book(side.book.topic_id))
                    except ApiError:
                        continue
                    self._remember(side.book)
                    side.held = self._held(side.book)
                return True
        return False

    def band_text(self, s: Side) -> str:
        """`band 75,800–78,600 (14 ranges) p 80%, 27 sats a set pays 3.6×`."""
        if s.band is None:
            return "no band"
        lo, hi = s.band
        return (f"band {pspan(s.book.bins[lo][0], s.book.bins[hi][1])} ({hi - lo + 1} range{'' if hi == lo else 's'}) p {s.band_p:.0%}, "
                f"{s.band_price:,.0f} sats a set pays {pays(s.band_p, s.band_price):.2f}×")

    def _why_nothing(self, v: Visit) -> str:
        best = max(self._traded(v), key=lambda s: s.edge)
        return f"no band {self.cfg.min_edge:.0%} over its price: {best.label}'s {self.band_text(best)}"

    def _plan(self, v: Visit, room: float) -> list[Side]:
        """What each leg buys of its band within `room`, the legs by their edge: a stake of fractional Kelly on the band
        as one bet, capped by the close's share of the budget less what it holds there, filled while the last set
        still returns min_edge. Sets each leg's `buys` and returns the legs with any."""
        planned: list[Side] = []
        for side in sorted(self._traded(v), key=lambda s: -s.edge):
            side.buys = []
            if side.band is None or room < 1:
                continue
            held_cost = self.ledger.open_cost(self.key, self.mode, side.book.topic_id)
            cap = self.cfg.edge_close_share * self.cfg.bankroll_sats - held_cost
            stake = min(room, cap, band_stake(side.band_p, side.band_price, self.cfg.kelly, self.cfg.bankroll_sats))
            d, cost = band_fill(side.book, *side.band, side.band_p, stake, self.cfg.min_edge)
            if d < 0.01 or cost < 1:
                continue
            legs = band_legs(side.book, *side.band, d, cost)
            if PL.worth_sending(legs, side.final):
                side.buys = legs
                planned.append(side)
                room -= cost
        return planned

    async def _bet(self, v: Visit) -> bool:
        """The statistical part: on each leg, the triangle's band where the book sells it under what it pays, every leg
        in one request."""
        tag = self.tag(v.end)
        books = {s.label: s.book for s in self._traded(v)}
        room, why = self._instant_room(books)
        if why:
            v.note = why
            self.say(f"{tag}  {self._gap_text(v)}  ·  {why}")
            return False
        planned = self._plan(v, room)
        if not planned:
            v.note = self._why_nothing(v)
            self.say(f"{tag}  {self._gap_text(v)}  ·  {v.note}")
            return False
        return await self._enter_many(v, planned)

    async def _enter_many(self, v: Visit, planned: list[Side]) -> bool:
        """Every leg's buy in one request. Live, each is checked against the server's estimate first and a leg whose
        book moved is left out; a leg the server refuses is dropped, the rest stand, since each is a bet of its own."""
        tag = self.tag(v.end)
        local = {s.label: sum(x.cost_sats for x in s.buys) for s in planned}
        orders = {s.label: [(s.book.option_ids[x.index], x.contracts) for x in s.buys] for s in planned}
        charges: dict[str, float] = {}
        if self.live:
            try:
                est = await asyncio.gather(*(self.api.estimate_legs(s.book.topic_id, orders[s.label]) for s in planned))
            except ApiError as e:
                self.say(f"{tag}  no estimate: {e}")
                return False
            keep = []
            for s, (raw, fee) in zip(planned, est, strict=True):
                charge = raw + max(fee, P.MIN_FEE_SATS)
                if abs(charge - local[s.label]) > self.cfg.estimate_tolerance * local[s.label] + 5:
                    self.say(f"{tag}  {s.label} book moved (local {local[s.label]:,.0f} vs server {charge:,.0f}), left out")
                    s.buys = []
                    continue
                keep.append(s)
            planned = keep
            if not planned:
                return False
            try:
                fills = await self.api.buy_many([(s.book.topic_id, orders[s.label]) for s in planned])
            except ApiError as e:
                self.say(f"{tag}  buy failed: {e}")
                if "may or may not" in str(e):
                    self.stop()
                return False
            by_topic = {f.topic_id: f for f in fills}
            keep = []
            for s in planned:
                f = by_topic.get(s.book.topic_id)
                if f is None or f.error:
                    self.say(f"{tag}  {s.label} rejected: {f.error if f else 'no result returned'}")
                    s.buys = []
                    continue
                charges[s.label] = f.cost_sats + f.fee_sats
                keep.append(s)
            planned = keep
            if not planned:
                return False
        else:
            charges = dict(local)
        parts, worth, total = [], 0.0, 0.0
        for s in planned:
            charge = charges[s.label]
            lo, hi = s.buys[0].index, s.buys[-1].index
            d = s.buys[0].contracts
            parts.append(f"{s.label} band {pspan(s.book.bins[lo][0], s.book.bins[hi][1])} ({len(s.buys)} range{'' if lo == hi else 's'}) "
                         f"{fmt.contracts(d)} a range for {fmt.sats(charge)}, pays {fmt.sats(PL.NET * d)} inside, p {s.band_p:.0%}")
            worth += sum(s.final[x.index] * PL.NET * x.contracts for x in s.buys)
            total += charge
            for x in s.buys:
                self.ledger.add(self.key, self.mode, s.book, s.label, x.index, x.contracts, charge * x.cost_sats / local[s.label])
            moved = _after(s.book, s.buys)
            self._mark(Picture(moved, s.final, 0.0))
            self._remember(moved)
            s.held = self._held(moved)
        self.spent.append((time.time(), total))
        self.paid_sats += total
        self.trades += 1
        self._round["bought"] += 1
        self._round["paid"] += total
        v.note = f"{'BUY' if self.live else 'would buy'} {' + '.join(parts)} for {fmt.sats(total)}, worth {fmt.sats(worth)} by the triangle"
        self.say(f"{tag}  {self._gap_text(v)}  ·  {v.note}")
        return True


def rules_text(r: Rules | None = None, t: Triangle = TRIANGLE) -> str:
    """The about screen's RUNNER section for a Triangulator of triangle `t`."""
    from .zoo.arbitrage import TRI_TRADING as c

    r = r or Rules()
    legs = (f"the two closes at one instant in {t.base} and {t.cross}, and {t.quote} in dollars as a model on the price those two "
            f"feeds imply (a Student-t whose variance adds up the hours to the close, each at oil's measured σ for the hours CME "
            f"trades or the hours it does not)" if t.synthetic else
            f"the three closes at one instant in {t.base}, {t.quote} and {t.cross}")
    return (
        f"Every few seconds it takes {legs}, within {r.max_hours:g} hours of now (the nearest instant every other visit, the rest "
        f"nearest to farthest), reads the books fresh, and draws its lens's picture of each: the {r.lens} model by default, a "
        "Student-t walk that adds up only the hours the asset's market trades, or any baseline, smoothing or Benter model by "
        f"choice. {t.cross} is {t.quote} × 10⁸ over {t.base}" + ("" if t.synthetic else ", times the basis the three spots show")
        + ", so for each leg it draws the picture the other two legs imply "
        f"({r.samples:,} Monte Carlo draws through a Gaussian copula whose correlation comes from the two feeds' hourly candles, "
        "binned on the leg's ladder) and pools it with the leg's own picture in log space, at a weight of up to "
        f"{r.cross_weight:g} that grows with how formed the other two books are and shrinks with how formed this one is. The pooled "
        "pictures are the triangle: one forecast of the instant. "
        + (f"It takes a risk-free cover first when a pair of the books offers one that returns {r.cover_edge:.0%} whichever way the "
           "price goes (the Arbitrageurs' maths, both legs in one request, a leg filled alone sold straight back). " if r.covers else "")
        + "Then on every leg with a market it sells what the market pays more for than the triangle says a position is worth and more "
        f"than it cost, and bets on the likely outcomes: the band is the narrowest run of ranges holding {r.band_mass:.0%} of the "
        "triangle's picture, the bet is the same number of contracts on every range of it (it pays 98 sats a set wherever inside "
        "the band the close lands, nothing outside), and the bot buys that set in every book that sells it for less than it pays. "
        f"The stake is fractional Kelly ({c['kelly']:g}) on the band as one bet, at most {c['edge_close_share']:.0%} of the budget in "
        f"one close and {r.instant_share:.0%} in one instant, and the fill stops at the set whose price no longer returns "
        f"{c['min_edge']:.0%} on cost after both fees: buying lifts a thin book, so it stops well short of its own fair value. All "
        "legs go in one request after checking each against the server's estimate. Near a close the books are sharper than the "
        "walk and the band costs more than it pays, so it buys nothing there; a day or two out the books sit near their flat seed "
        "and sell the band cheap, gold's weekend closes cheapest, and that is where it buys. Every visit logs how far the sats book "
        "sits from what the dollar books imply, by the market, by the lens and by the triangle, each leg's band, its chance and "
        "what a set costs, and what it bought. Settings live in a [triangle] table of ~/.config/glimpse/bots.toml (lens, "
        "cross_weight, band_mass, max_hours, instant_share, covers, cover_edge, samples, smooth)."
    )
