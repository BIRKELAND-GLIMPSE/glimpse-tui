"""MINE, HASH, DIFF, HASHP, HALV, SUPL, LN and ORCL: who mines, how hard, what it pays, what is left to issue,
the Lightning network, and the price read from the chain alone.

Every pane loads through the hub's Bitcoin backends in the user's order and names the one that answered. Two
places do not fall back quietly: DIFF's estimate (Bitview's disagrees with mempool's, SOURCES.md) and the series and
oracle that only Bitview serves. Supply, the halving and hashprice are computed here from `data/btcmath.py`.
"""
from __future__ import annotations

import math
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from rich.cells import cell_len
from rich.text import Text

from .. import charts, fmt
from ..data import bitview, btcmath
from ..data.bitview import Bitview, SeriesData
from ..data.core import DAILY, Provenance, SourceError
from ..term import ui
from ..term.panes import FuncPane
from ..term.registry import Function, register
from ..theme import DIM, FAINT, GREEN, ORANGE, RED, TEXT

CYAN = charts.CYAN
TRACK = "#3a3a3a"
POOL_PERIODS = ("24h", "3d", "1w", "1m", "3m", "6m", "1y", "2y", "3y")
HASH_PERIODS = ("3m", "6m", "1y", "2y", "3y", "all")
REVENUE_PERIODS = ("24h", "3d", "1w", "1m", "3m")
LN_PERIODS = ("1m", "3m", "6m", "1y", "2y", "3y")
LN_STALE_S = 3 * 86400.0
ROUND_DOLLARS = (10, 20, 50, 100, 200, 500, 1000)
# When each subsidy era began, UTC. Later eras are estimated from the tip at the ten-minute target.
ERA_STARTS = {0: datetime(2009, 1, 3, tzinfo=UTC), 1: datetime(2012, 11, 28, tzinfo=UTC), 2: datetime(2016, 7, 9, tzinfo=UTC),
              3: datetime(2020, 5, 11, tzinfo=UTC), 4: datetime(2024, 4, 20, tzinfo=UTC)}
LAST_ERA = 32                   # the last era that pays anything: one sat a block


# ── the maths the pages share ───────────────────────────────

def pool_shares(pools: Sequence[dict[str, Any]]) -> list[float]:
    """Each pool's share of the blocks in the window. Pool rows carry no share field, so it is blocks over the total."""
    total = sum(int(p.get("blockCount", 0)) for p in pools)
    return [int(p.get("blockCount", 0)) / total if total else 0.0 for p in pools]


def network_hashrate(data: dict[str, Any], period: str) -> float:
    """The network estimate that matches the window: the 24 hour one, the three day one, or the week's for anything longer."""
    key = {"24h": "lastEstimatedHashrate", "3d": "lastEstimatedHashrate3d"}.get(period, "lastEstimatedHashrate1w")
    return float(data.get(key) or data.get("lastEstimatedHashrate") or 0.0)


def blocks_per_day(points: Sequence[dict[str, Any]]) -> float | None:
    """The observed block rate over a rewards or fees window, from the first and last points' heights and block times."""
    pts = [p for p in points if p.get("avgHeight") is not None and p.get("timestamp")]
    if len(pts) < 2:
        return None
    blocks, seconds = float(pts[-1]["avgHeight"]) - float(pts[0]["avgHeight"]), float(pts[-1]["timestamp"]) - float(pts[0]["timestamp"])
    return blocks / seconds * 86400.0 if blocks > 0 and seconds > 0 else None


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def change_over(values: Sequence[float], days: int, smooth: int = 7) -> float | None:
    """The change of a noisy daily series over `days`: the mean of the last `smooth` points against the mean of the
    `smooth` points that ended `days` earlier."""
    if len(values) < days + smooth:
        return None
    then = mean(values[-days - smooth:-days])
    return mean(values[-smooth:]) / then - 1 if then > 0 else None


def trailing_mean(xs: Sequence[float], ys: Sequence[float], span: float) -> list[float]:
    """The mean of the points in the `span` before each x, itself included: a seven day mean of daily estimates."""
    out, total, first = [], 0.0, 0
    for i, (x, y) in enumerate(zip(xs, ys, strict=True)):
        total += y
        while xs[first] <= x - span:
            total -= ys[first]
            first += 1
        out.append(total / (i - first + 1))
    return out


def era_rows(height: int, now: float) -> list[dict[str, Any]]:
    """The whole issuance schedule: one row an era until the subsidy reaches zero."""
    out = []
    for era in range(LAST_ERA + 1):
        start = era * btcmath.HALVING_INTERVAL
        end = start + btcmath.HALVING_INTERVAL - 1
        known = ERA_STARTS.get(era) if start <= height else None
        at = known.timestamp() if known else now + (start - height) * btcmath.TARGET_SPACING_S
        out.append({"era": era + 1, "start": start, "subsidy": btcmath.subsidy_sats(start), "at": at, "known": known is not None,
                    "supply": btcmath.issued_sats(end) / 1e8, "share": btcmath.issued_sats(end) / 1e8 / 21e6})
    return out


def oracle_columns(hist: Sequence[int], price: float, width: int, lo_usd: float = 1.0, hi_usd: float = 10_000.0
                   ) -> tuple[list[float], int, int]:
    """The histogram between two dollar amounts at `price`, folded to at most `width` columns. Each column is the
    tallest bin of its slice, so a one-bin spike stays a spike. Returns (columns, first bin, bins a column)."""
    lo, hi = max(bitview.oracle_bin(lo_usd, price), 0), min(bitview.oracle_bin(hi_usd, price), len(hist) - 1)
    if hi <= lo or width <= 0:
        return [], lo, 1
    per = max(math.ceil((hi - lo + 1) / width), 1)
    return [float(max(hist[i:min(i + per, hi + 1)])) for i in range(lo, hi + 1, per)], lo, per


def spike_ratio(hist: Sequence[int], at: int, reach: int = 12) -> float | None:
    """How far a bin stands above its neighbourhood: its count over the median of the bins around it."""
    if not 0 <= at < len(hist):
        return None
    near = sorted(hist[i] for i in range(max(at - reach, 0), min(at + reach + 1, len(hist))) if i != at)
    med = near[len(near) // 2] if near else 0
    return hist[at] / med if med > 0 else None


# ── formatting ──────────────────────────────────────────────

def clean(s: Any, limit: int = 40) -> str:
    """A pool or node name with anything that is not one cell wide removed: aliases carry emoji and CJK."""
    return "".join(ch for ch in str(s or "") if ch.isprintable() and cell_len(ch) == 1)[:limit].strip() or "?"


def dur(seconds: float) -> str:
    s = int(max(seconds, 0))
    d, r = divmod(s, 86400)
    h, r = divmod(r, 3600)
    m, sec = divmod(r, 60)
    if d:
        return f"{d:,}d {h}h"
    return f"{h}h {m:02d}m" if h else f"{m}m {sec:02d}s"


def coins(sats: float, digits: int = 2) -> str:
    return f"{sats / 1e8:,.{digits}f} BTC"


def dollar_label(usd: float) -> str:
    return f"${usd / 1000:g}k" if usd >= 1000 else f"${usd:g}"


def track(frac: float, width: int, colour: str = ORANGE) -> Text:
    """A progress bar on a faint track, eighth-block fine."""
    body = charts.hbar(frac, width)
    return ui.t((body, charts.snap(colour)), ("░" * max(width - len(body), 0), TRACK))


@dataclass
class C:
    """A column of `grid`. `drop` 0 stays; otherwise the highest number leaves first when the pane is narrow.
    `shrink` lets a text column be cut to that many cells before anything is dropped."""
    head: str
    align: str = "right"
    style: str = TEXT
    drop: int = 0
    shrink: int = 0


@dataclass
class Bar:
    frac: float
    colour: str = ORANGE


BAR_MIN = 6


def grid(specs: Sequence[C], rows: Sequence[Sequence[Any]], w: int, cursor: int | None = None, gap: int = 2, bar_max: int = 30) -> list[Text]:
    """`ui.table` that fits: columns leave in `drop` order until the rest fit `w`, text columns give up width first,
    and bar cells are drawn last at whatever width is left. Never wider than `w`."""
    n = len(specs)
    cells = [[c if isinstance(c, Text | Bar) else Text("" if c is None else str(c)) for c in list(r)[:n]] + [Text("")] * (n - len(r))
             for r in rows]
    is_bar = [any(isinstance(r[i], Bar) for r in cells) for i in range(n)]
    nat = [BAR_MIN if is_bar[i] else max([len(s.head)] + [r[i].cell_len for r in cells if isinstance(r[i], Text)]) for i, s in enumerate(specs)]
    low = [max(len(s.head), min(nat[i], s.shrink)) if s.shrink else nat[i] for i, s in enumerate(specs)]
    keep = list(range(n))

    def need() -> int:
        return sum(low[i] for i in keep) + gap * (len(keep) - 1)

    while need() > w and any(specs[i].drop for i in keep):
        keep.remove(max((i for i in keep if specs[i].drop), key=lambda i: specs[i].drop))
    spare, width = max(w - need(), 0), {i: low[i] for i in keep}
    for i in keep:
        if specs[i].shrink:
            add = min(spare, nat[i] - low[i])
            width[i], spare = width[i] + add, spare - add
    bars = [i for i in keep if is_bar[i]]
    for i in bars:
        width[i] += min(spare // len(bars), bar_max - BAR_MIN)
    cols = [ui.Col(specs[i].head, specs[i].align, width[i], specs[i].style) for i in keep]
    body = [[Text(charts.hbar(r[i].frac, width[i]), style=charts.snap(r[i].colour)) if isinstance(r[i], Bar) else r[i] for i in keep]
            for r in cells]
    return clip(ui.table(cols, body, w, cursor, gap), w)


def clip(lines: Sequence[Text], w: int) -> list[Text]:
    """No line is ever wider than the pane: a line that would be is cut, never wrapped."""
    out = []
    for ln in lines:
        if ln.cell_len > w:
            ln = ln.copy()
            ln.truncate(w)
        out.append(ln)
    return out


def row(w: int, *chunks: Text | tuple[str, str] | str) -> Text:
    """As many of the chunks as fit in `w`, in order: a summary line that sheds its tail on a narrow pane."""
    out = Text(no_wrap=True, overflow="crop")
    for ch in chunks:
        piece = ch if isinstance(ch, Text) else Text(*ch) if isinstance(ch, tuple) else Text(ch)
        if out.cell_len + piece.cell_len > w:
            break
        out.append_text(piece)
    return out


def beside(left: Sequence[Text], right: Sequence[Text], left_w: int, gap: int = 3) -> list[Text]:
    out = []
    for i in range(max(len(left), len(right))):
        ln = left[i].copy() if i < len(left) else Text("", no_wrap=True)
        ln.truncate(left_w, pad=True)
        ln.append(" " * gap)
        if i < len(right):
            ln.append_text(right[i])
        out.append(ln)
    return out


def plot_lines(plot: charts.Plot) -> list[Text]:
    return list(plot.render().split("\n"))


# ── what the panes share ────────────────────────────────────

class MiningPane(FuncPane):
    """Backend resolution, the tip height and the dollar price, the same way on every page of the family."""

    def __init__(self, hub, security=None, args=()) -> None:
        super().__init__(hub, security, args)
        self.height = 0
        self.da: dict[str, Any] = {}
        self.alt_price: float | None = None
        self.price_prov: Provenance | None = None

    async def ask(self, method: str, *args: Any, trusted: bool = False) -> tuple[Any, Provenance]:
        """The first backend in the user's order that has `method` and answers. `trusted` skips Bitview, whose
        difficulty estimate disagrees with mempool's."""
        last: SourceError | None = None
        for key in self.hub.bitcoin_order():
            src = self.hub.sources[key]
            if trusted and isinstance(src, Bitview):
                continue
            try:
                return await getattr(src, method)(*args)
            except SourceError as e:
                last = e
            except AttributeError:
                continue
        raise last or SourceError(f"no Bitcoin backend answers {method}")

    async def fetch(self, path: str, ttl: float) -> tuple[Any, Provenance]:
        """A mempool-shaped path the client has no method for yet."""
        last: SourceError | None = None
        for key in self.hub.bitcoin_order():
            src = self.hub.sources[key]
            try:
                v, at = await src.get(path, ttl=ttl)
                return v, src.prov(at)
            except SourceError as e:
                last = e
        raise last or SourceError("no Bitcoin backend answered")

    async def try_(self, method: str, *args: Any, trusted: bool = False) -> tuple[Any, Provenance | None]:
        try:
            return await self.ask(method, *args, trusted=trusted)
        except SourceError:
            return None, None

    async def load_tip(self) -> list[Provenance]:
        """The tip and this epoch's pace come from the hub's stream when it runs; otherwise one request each."""
        provs: list[Provenance] = []
        if not self.hub.chain.height:
            v, p = await self.try_("tip_height")
            if v:
                self.height = int(v)
                provs.append(p)
        if not self.hub.chain.difficulty:
            v, p = await self.try_("difficulty_adjustment", trusted=True)
            if v:
                self.da = v
        return provs

    async def load_price(self) -> None:
        """The composite BTC quote is the price. Without it, mempool's own price stands in and the page says so."""
        if self.hub.btc_price() is None:
            v, p = await self.try_("prices")
            if v and v.get("USD"):
                self.alt_price, self.price_prov = float(v["USD"]), p

    def tip(self) -> int:
        return max(self.hub.chain.height, self.height)

    def epoch(self) -> dict[str, Any]:
        return self.hub.chain.difficulty or self.da

    def spacing(self) -> tuple[float, bool]:
        """(seconds a block, observed?) for this difficulty epoch; the ten-minute target when nothing was observed."""
        avg = float(self.epoch().get("timeAvg") or 0) / 1000.0
        return (avg, True) if avg > 0 else (float(btcmath.TARGET_SPACING_S), False)

    def price(self) -> tuple[float | None, str, Provenance | None]:
        q = self.hub.quotes.get("BTC")
        if q:
            return q.price, "composite", q.prov
        return self.alt_price, (self.price_prov.source if self.price_prov else ""), self.price_prov

    def chain_provs(self) -> list[Provenance]:
        return [self.hub.chain.prov] if self.hub.chain.prov else []


class PeriodPane(MiningPane):
    """A page with a window: the first argument sets it, [ and ] step through it, and a saved launchpad keeps it."""
    PERIODS: tuple[str, ...] = ()
    DEFAULT = ""

    def __init__(self, hub, security=None, args=()) -> None:
        super().__init__(hub, security, args)
        named = [a.lower() for a in self.args if a.lower() in self.PERIODS]
        self.period = named[0] if named else self.DEFAULT
        self.shown = self.period                            # the window the data on screen belongs to, until the next load lands

    def step(self, ch: str | None) -> bool:
        if ch not in ("[", "]"):
            return False
        i = self.PERIODS.index(self.period) + (1 if ch == "]" else -1)
        self.period = self.PERIODS[max(0, min(i, len(self.PERIODS) - 1))]
        self.loaded_at = 0.0                                # due at the shell's next tick; the old picture stays until then
        return True

    def key(self, k: str, ch: str | None) -> bool:
        return self.step(ch)

    def command(self) -> str:
        return f"{self.code} {self.period}"

    def cache_key(self) -> tuple:
        return (self.period,)


# ── MINE ────────────────────────────────────────────────────

class MinePane(PeriodPane):
    code, every, selectable = "MINE", 600, True
    PERIODS, DEFAULT = POOL_PERIODS, "1w"

    def __init__(self, hub, security=None, args=()) -> None:
        super().__init__(hub, security, args)
        others = [a.lower() for a in self.args if a.lower() not in self.PERIODS]
        self.slug = others[0] if others else ""
        self.data: dict[str, Any] = {}
        self.pool: dict[str, Any] = {}
        self.blocks: list[dict[str, Any]] = []
        self.history: list[dict[str, Any]] = []

    async def load(self) -> None:
        if not self.slug:
            self.title = f"pools · {self.period}"
            self.data, p = await self.ask("pools", self.period)
            self.shown = self.period
            self.provs = [p]
            return
        self.title = f"pool · {self.slug}"
        self.pool, p = await self.ask("pool", self.slug)
        self.title = f"pool · {clean((self.pool.get('pool') or {}).get('name') or self.slug)}"
        provs = [p]
        blocks, p2 = await self.try_("pool_blocks", self.slug)
        self.blocks = blocks or []
        try:
            hist, _ = await self.fetch(f"/api/v1/mining/pool/{self.slug}/hashrate", 3600)
            self.history = hist if isinstance(hist, list) else []
        except SourceError:
            self.history = []
        await self.load_price()
        self.provs = provs + ([p2] if p2 else []) + ([self.price_prov] if self.price_prov else [])

    # the list ───────────────────────────────────────────────

    def pools(self) -> list[dict[str, Any]]:
        return list(self.data.get("pools") or [])

    def draw(self, w: int, h: int) -> list[Text]:
        return self.draw_pool(w, h) if self.slug else self.draw_list(w, h)

    def draw_list(self, w: int, h: int) -> list[Text]:
        pools = self.pools()
        self.n_rows = len(pools)
        if not pools:
            return []
        shares, net = pool_shares(pools), network_hashrate(self.data, self.shown)
        total = sum(int(p.get("blockCount", 0)) for p in pools)
        acc, majority = 0.0, 0
        for i, s in enumerate(shares):
            acc += s
            if acc > 0.5:
                majority = i + 1
                break
        out = [row(w, (f"{self.shown}  ", f"bold {ORANGE}"), (f"{total:,} blocks", f"bold {TEXT}"),
                   (f" · {len(pools)} pools", DIM), (f" · {ui.hashrate(net)}", DIM) if net else "",
                   (f" · top 3 mine {sum(shares[:3]):.1%}", DIM), (f" · {majority} pools pass 50%", FAINT) if majority else "")]
        top = max(shares) or 1.0
        rows = []
        for p, s in zip(pools, shares, strict=True):
            health = p.get("avgMatchRate")
            delta = p.get("avgFeeDelta")
            empty = Text(str(p.get("emptyBlocks", 0)), style=RED if p.get("emptyBlocks") else FAINT)
            rows.append([str(p.get("rank", "")), clean(p.get("name")), f"{int(p.get('blockCount', 0)):,}", f"{s:.1%}", Bar(s / top),
                         f"{s * net / 1e18:,.1f}" if net else "–", empty,
                         f"{float(health):.2f}%" if health is not None else "–",
                         ui.signed_pct(float(delta), 2) if delta not in (None, "") else Text("–", style=FAINT)])
        specs = [C("#", style=FAINT), C("pool", "left", f"bold {TEXT}", shrink=10), C("blocks"), C("share", style=f"bold {ORANGE}"),
                 C("", "left", drop=2), C("EH/s", drop=3), C("empty", drop=6), C("health", style=DIM, drop=4), C("fee delta", drop=5)]
        out += grid(specs, rows, w, self.cur)
        out.append(ui.note("share = blocks over all blocks in the window · EH/s = share of the network estimate · health = audit match"))
        self.follow(self.cur + 3, h, head=3)
        return clip(out, w)

    # one pool ───────────────────────────────────────────────

    def draw_pool(self, w: int, h: int) -> list[Text]:
        d, now = self.pool, time.time()
        if not d:
            self.n_rows = 0
            return []
        price = self.price()[0]
        info, counts, shares = d.get("pool") or {}, d.get("blockCount") or {}, d.get("blockShare") or {}
        est = float(d.get("estimatedHashrate") or 0)
        out = [row(w, (clean(info.get("name") or self.slug), f"bold {ORANGE}"),
                   (f"  {ui.hashrate(est)} estimated", f"bold {TEXT}") if est else "",
                   (f"  block health {float(d['avgBlockHealth']):.1f}%", DIM) if d.get("avgBlockHealth") is not None else "",
                   (f"  {info.get('link', '')}", FAINT))]
        if d.get("totalReward"):
            reward = float(d["totalReward"])
            out.append(row(w, ui.kv("all rewards", coins(reward, 0), f"bold {ORANGE}"),
                           (f" ({ui.usd(reward / 1e8 * price)} today)", DIM) if price else "",
                           (f"  {int(counts.get('all', 0)):,} blocks since the first", FAINT)))
        top = max([float(v) for v in shares.values()] or [1.0]) or 1.0
        wins = [[k, f"{int(counts.get(k, 0)):,}", f"{float(shares.get(k, 0)):.2%}", Bar(float(shares.get(k, 0)) / top)]
                for k in ("24h", "1w", "all") if k in counts]
        out += [Text("")] + grid([C("window", "left", DIM), C("blocks"), C("share", style=f"bold {ORANGE}"), C("", "left", drop=1)], wins, w,
                                 bar_max=40)
        if len(self.history) >= 2:
            hs, sh = [float(x.get("avgHashrate", 0)) for x in self.history], [float(x.get("share", 0)) for x in self.history]
            sw = max(min(w - 40, 40), 8)
            out += [Text(""), row(w, ("weekly EH/s  ", FAINT), (charts.spark(hs, sw), ORANGE), (f"  {hs[-1] / 1e18:,.1f} now", DIM),
                                  (f"  since {ui.day(float(self.history[0].get('timestamp', 0)))}", FAINT)),
                    row(w, ("weekly share ", FAINT), (charts.spark(sh, sw), CYAN), (f"  {sh[-1]:.1%} now", DIM))]
        self.n_rows = len(self.blocks)
        if self.blocks:
            out += [Text(""), ui.section("RECENT BLOCKS", w, "enter opens BLK")]
            head = len(out)
            rows = []
            for b in self.blocks:
                ex, ts = b.get("extras") or {}, float(b.get("timestamp", 0))
                match = ex.get("matchRate")
                rows.append([Text(f"{int(b.get('height', 0)):,}", style=f"bold {ORANGE}"), ui.when(ts, now), ui.stamp(ts),
                             f"{int(b.get('tx_count', 0)):,}",
                             fmt.sats(float(ex.get("totalFees", 0))), fmt.sats(float(ex.get("reward", 0))),
                             ui.usd(float(ex.get("reward", 0)) / 1e8 * price) if price else "–", f"{float(ex.get('medianFee', 0)):g}",
                             f"{float(match):.2f}%" if match is not None else "–"])
            specs = [C("height"), C("age", style=DIM), C("mined", style=FAINT, drop=6), C("txs"), C("fees"), C("reward", drop=2),
                     C("reward USD", style=DIM, drop=5), C("median s/vB", style=DIM, drop=4), C("match", style=DIM, drop=3)]
            out += grid(specs, rows, w, self.cur)
            self.follow(head + 2 + self.cur, h, head=3)
        return clip(out, w)

    def key(self, k: str, ch: str | None) -> bool:
        return False if self.slug else self.step(ch)

    def command(self) -> str:
        return f"MINE {self.slug}" if self.slug else f"MINE {self.period}"

    def enter(self) -> str | None:
        if self.slug:
            return f"BLK {int(self.blocks[self.cur]['height'])}" if self.cur < len(self.blocks) else None
        pools = self.pools()
        return f"MINE {pools[self.cur].get('slug')}" if self.cur < len(pools) and pools[self.cur].get("slug") else None

    def hint(self) -> str:
        return "enter opens the block · MINE for every pool" if self.slug else f"[ ] window ({self.period}) · enter opens the pool"

    def menu(self) -> list[tuple[str, str]]:
        return [("MINE", "MINE"), ("HASH", "HASH"), ("DIFF", "DIFF"), ("HASHP", "HASHP"), ("BLK", "BLK")]

    def export(self):
        if self.slug:
            return (["height", "timestamp", "tx_count", "total_fees_sat", "reward_sat", "median_fee", "match_rate"],
                    [[b.get("height"), b.get("timestamp"), b.get("tx_count"), (b.get("extras") or {}).get("totalFees"),
                      (b.get("extras") or {}).get("reward"), (b.get("extras") or {}).get("medianFee"), (b.get("extras") or {}).get("matchRate")]
                     for b in self.blocks])
        pools = self.pools()
        net = network_hashrate(self.data, self.shown)
        return (["rank", "pool", "slug", "blocks", "share", "est_hashrate_hs", "empty_blocks", "avg_match_rate", "avg_fee_delta"],
                [[p.get("rank"), p.get("name"), p.get("slug"), p.get("blockCount"), round(s, 6), s * net, p.get("emptyBlocks"),
                  p.get("avgMatchRate"), p.get("avgFeeDelta")] for p, s in zip(pools, pool_shares(pools), strict=True)])


# ── HASH ────────────────────────────────────────────────────

def difficulty_steps(rows: Sequence[dict[str, Any]], x_lo: float, x_hi: float) -> tuple[list[float], list[float]]:
    """Difficulty is flat between retargets: each adjustment becomes a step, from the window's left edge to its right."""
    rows = sorted(rows, key=lambda r: float(r.get("time", 0)))
    if not rows:
        return [], []
    first = rows[0]
    before = float(first["difficulty"]) / float(first.get("adjustment") or 1.0)
    xs, ys = [min(x_lo, float(first["time"]))], [before]
    for r in rows:
        xs += [float(r["time"]), float(r["time"])]
        ys += [ys[-1], float(r["difficulty"])]
    if x_hi > xs[-1]:
        xs.append(x_hi)
        ys.append(ys[-1])
    return xs, ys


class HashPane(PeriodPane):
    code, every = "HASH", 1800
    PERIODS, DEFAULT = HASH_PERIODS, "1y"

    def __init__(self, hub, security=None, args=()) -> None:
        super().__init__(hub, security, args)
        self.data: dict[str, Any] = {}
        self.log_y = False
        self.cross: SeriesData | None = None

    async def load(self) -> None:
        self.title = f"hashrate · {self.period}"
        self.data, p = await self.ask("hashrate", self.period)
        self.shown = self.period
        self.provs = [p]
        self.cross = None
        name = (bitview.onchain_catalogue().get("metrics", {}).get("hash_rate") or {}).get("series")
        if name and (bv := self.hub.sources.get("bitview")):
            try:
                self.cross = await bv.series(name, "day1", start=-60)
                self.provs = [p, self.cross.prov]
            except (SourceError, AttributeError):
                pass

    def draw(self, w: int, h: int) -> list[Text]:
        d = self.data
        pts = [(float(x["timestamp"]), float(x["avgHashrate"]) / 1e18) for x in d.get("hashrates") or [] if x.get("avgHashrate")]
        if not pts:
            return []
        shown = self.shown
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        week = trailing_mean(xs, ys, 7 * 86400.0)           # a day's estimate swings 15% on luck alone; the week's mean is the signal
        cur, diff = float(d.get("currentHashrate") or 0) / 1e18, float(d.get("currentDifficulty") or 0)
        ripe = next((i for i, x in enumerate(xs) if x - xs[0] >= 6 * 86400.0), 0)       # before this the mean has under a week in it
        hi_v, hi_t = max(zip(week[ripe:], xs[ripe:], strict=True))
        high = "all-time high" if shown == "all" else f"{shown} high"
        now_, chg = [ui.kv("hashrate", f"{cur:,.1f} EH/s", f"bold {ORANGE}"), ui.kv("   difficulty", f"{diff / 1e12:,.2f} T", f"bold {CYAN}")], [
            ("7d ", FAINT), ui.signed_pct(change_over(ys, 7)), ("   30d ", FAINT), ui.signed_pct(change_over(ys, 30))]
        peak = row(w, ui.kv(high, f"{hi_v:,.1f} EH/s"), (f" {ui.day(hi_t)}", DIM), ("  now ", FAINT),
                   ui.signed_pct(week[-1] / hi_v - 1 if hi_v else None), ("  7 day means", FAINT))
        out = [row(w, *now_, ("   ", ""), *chg), peak] if w >= 70 else [row(w, *now_), row(w, *chg), peak]
        tail = self.table(ys, week[ripe:], cur, d, w)
        ph = min(h - len(out) - 1 - (len(tail) if h >= 26 else 0), 24)
        if ph >= 4:
            rx, ry = charts.resample(xs, week, max(w * 2, 24))
            plot = charts.Plot(w, ph, log=self.log_y).line(rx, ry, ORANGE, label="hashrate, EH/s, 7 day mean" if w >= 70 else "EH/s, 7d mean")
            dx, dy = difficulty_steps(d.get("difficulty") or [], xs[0], xs[-1])
            if dx:
                plot.line(dx, [v / 1e12 for v in dy], CYAN, axis=1, label="difficulty, T" if w >= 70 else "diff, T")
            legend = plot.legend()
            legend.rstrip()
            out += plot_lines(plot) + [row(w, legend, ("   log scale" if self.log_y else "", FAINT))]
        return clip(out + tail, w)

    def table(self, ys: Sequence[float], week: Sequence[float], cur: float, d: dict[str, Any], w: int) -> list[Text]:
        rows = [["hashrate, EH/s", f"{cur:,.1f}", f"{week[-1]:,.1f}", f"{mean(ys[-30:]):,.1f}", f"{max(week):,.1f}", f"{min(week):,.1f}"]]
        ds = [float(r["difficulty"]) / 1e12 for r in d.get("difficulty") or [] if r.get("difficulty")]
        if ds:
            now_d = float(d.get("currentDifficulty") or ds[-1] * 1e12) / 1e12
            rows.append(["difficulty, T", f"{now_d:,.2f}", "", "", f"{max(ds):,.2f}", f"{min(ds):,.2f}"])
        specs = [C("", "left", DIM), C("now", style=f"bold {TEXT}"), C("7d mean", drop=2), C("30d mean", drop=1), C("high"), C("low")]
        out = [Text("")] + grid(specs, rows, w)
        if self.cross and self.cross.settled():
            day = self.cross.times[-2] if len(self.cross.values) > 1 else self.cross.times[-1]
            out.append(row(w, ui.kv("bitview hash_rate", f"{self.cross.settled() / 1e18:,.1f} EH/s"), (f"  daily · {ui.day(day)}", DIM),
                           ("  a second estimate, from the chain alone", FAINT)))
        return out

    def key(self, k: str, ch: str | None) -> bool:
        if ch in ("L", "l"):
            self.log_y = not self.log_y
            return True
        return self.step(ch)

    def hint(self) -> str:
        return f"[ ] window ({self.period}) · L log scale"

    def menu(self) -> list[tuple[str, str]]:
        return [("DIFF", "DIFF"), ("MINE", "MINE"), ("HASHP", "HASHP"), ("HALV", "HALV")]

    def export(self):
        diffs = sorted(self.data.get("difficulty") or [], key=lambda r: float(r.get("time", 0)))
        before = float(diffs[0]["difficulty"]) / float(diffs[0].get("adjustment") or 1.0) if diffs else None
        rows = []
        for x in self.data.get("hashrates") or []:
            at = [r for r in diffs if float(r.get("time", 0)) <= float(x["timestamp"])]
            rows.append([int(x["timestamp"]), ui.day(float(x["timestamp"])), x.get("avgHashrate"), at[-1]["difficulty"] if at else before])
        return ["timestamp", "day_utc", "avg_hashrate_hs", "difficulty"], rows


# ── DIFF ────────────────────────────────────────────────────

class DiffPane(MiningPane):
    code, every, tick = "DIFF", 60, 1.0

    def __init__(self, hub, security=None, args=()) -> None:
        super().__init__(hub, security, args)
        self.now_da: dict[str, Any] = {}
        self.unsafe = ""                    # set to the backend's name when only the one SOURCES.md distrusts answered
        self.past: list[list[float]] = []
        self.da_prov: Provenance | None = None
        self.past_prov: Provenance | None = None

    async def load(self) -> None:
        self.title = "difficulty epoch"
        self.unsafe = ""
        try:
            self.now_da, self.da_prov = await self.ask("difficulty_adjustment", trusted=True)
        except SourceError as first:
            # Bitview's difficultyChange read -24.9% when mempool.space said -6.0% at the same moment. It may stand in for
            # the block counts, which are arithmetic on the height, but its estimate is shown as unverified, never as the number.
            bv = next((self.hub.sources[k] for k in self.hub.bitcoin_order() if isinstance(self.hub.sources[k], Bitview)), None)
            if bv is None:
                raise
            try:
                self.now_da, self.da_prov = await bv.difficulty_adjustment()
            except SourceError:
                raise first from None
            self.unsafe = bv.name
        past, self.past_prov = await self.try_("difficulty_adjustments", "1y", trusted=True)
        self.past = [list(r) for r in past or [] if isinstance(r, list | tuple) and len(r) >= 4]
        self.provs = [p for p in (self.current()[1], self.past_prov) if p]

    def cache_key(self) -> tuple:
        return (self.hub.chain.height, self.hub.chain.streaming)

    def current(self) -> tuple[dict[str, Any], Provenance | None]:
        c = self.hub.chain
        if c.streaming and c.difficulty and not self.unsafe:
            return c.difficulty, c.prov
        return self.now_da, self.da_prov

    def draw(self, w: int, h: int) -> list[Text]:
        da, prov = self.current()
        self.provs = [p for p in (prov, self.past_prov) if p]
        if not da:
            return []
        now = time.time()
        left = int(da.get("remainingBlocks", 0))
        done = btcmath.RETARGET_INTERVAL - left
        eta = float(da.get("estimatedRetargetDate", 0)) / 1000.0                 # milliseconds at the source
        avg = float(da.get("timeAvg", 0)) / 1000.0                               # milliseconds
        started = float(da.get("previousTime", 0))                               # seconds
        chg, prev = float(da.get("difficultyChange", 0)) / 100, float(da.get("previousRetarget", 0)) / 100
        out = [ui.section("CURRENT EPOCH", w, f"retarget at #{int(da.get('nextRetargetHeight', 0)):,}")]
        pct = f" {done / btcmath.RETARGET_INTERVAL:6.2%}"
        out.append(ui.t(track(done / btcmath.RETARGET_INTERVAL, max(w - len(pct), 8)), (pct, f"bold {TEXT}")))
        if self.unsafe:
            out += ui.wrap(f"UNVERIFIED: only {self.unsafe} answered. Its estimate has read far from mempool's (SOURCES.md).", w, f"bold {RED}")
        est = ui.signed_pct(chg, 2)
        if self.unsafe:
            est = Text(f"{est.plain} unverified", style=RED)
        behind = done - float(da.get("expectedBlocks", done))
        pace = f"{abs(behind):,.0f} blocks {'ahead of' if behind > 0 else 'behind'} schedule" if abs(behind) >= 1 else "on schedule"
        cur_d = float(self.past[0][2]) if self.past else 0.0
        left_col = [ui.kv("blocks done    ", f"{done:,} of {btcmath.RETARGET_INTERVAL:,}"),
                    ui.kv("blocks left    ", f"{left:,}", f"bold {ORANGE}"),
                    ui.kv("avg block time ", f"{dur(avg)}" if avg else "–"),
                    ui.kv("epoch began    ", ui.stamp(started) if started else "–", DIM),
                    ui.kv("difficulty now ", f"{cur_d / 1e12:,.2f} T" if cur_d else "–")]
        nxt = (f"  to {cur_d * (1 + chg) / 1e12:,.2f} T", DIM) if cur_d and not self.unsafe else ""
        right_col = [ui.t(ui.kv("estimated change", Text("")), est, nxt),
                     ui.t(ui.kv("previous change ", Text("")), ui.signed_pct(prev, 2)),
                     ui.kv("retarget ETA    ", f"{ui.stamp(eta)}" if eta else "–"), ui.kv("time to retarget", dur(eta - now) if eta else "–"),
                     ui.kv("against 10m 00s ", pace, DIM)]
        out += beside(left_col, right_col, 34) if w >= 78 else left_col + right_col
        if self.past:
            out += self.history(w, h - len(out))
        return clip(out, w)

    def history(self, w: int, room: int) -> list[Text]:
        past = self.past                                            # newest first: [time s, height, difficulty, ratio]
        out = [Text(""), ui.section("PAST ADJUSTMENTS", w, f"{len(past)} in a year")]
        if room >= 14:
            old = list(reversed(past))
            per = max(min(w // len(old), 4), 1)                   # a bar is up to three cells and a gap; one cell on a narrow pane
            old = old[-(w // per):]
            vals, cols = [], []
            for r in old:
                c = float(r[3]) - 1
                vals += [abs(c)] * (per - 1 if per > 1 else 1) + ([0.0] if per > 1 else [])
                cols += [GREEN if c >= 0 else RED] * per
            out += charts.vbars(vals, min(max(room - 12, 3), 6), colours=cols)
            first, last, mid = ui.day(float(old[0][0])), ui.day(float(old[-1][0])), f"tallest {max(vals):.1%} · green up, red down"
            gapw = len(vals) - len(first) - len(last)
            axis = first + mid.center(gapw) + last if gapw >= len(mid) + 2 else (first + " " * max(gapw, 1) + last if gapw >= 1 else last)
            out.append(ui.note(axis))
        rows = []
        for i, r in enumerate(past):
            c = float(r[3]) - 1
            secs = (float(past[i - 1][0]) if i else None)
            span = secs - float(r[0]) if secs else None             # how long the epoch this retarget opened then lasted
            rows.append([ui.day(float(r[0])), f"{int(r[1]):,}", f"{float(r[2]) / 1e12:,.2f} T", ui.signed_pct(c, 2),
                         Bar(min(abs(c) / 0.12, 1.0), GREEN if c >= 0 else RED), f"{span / 86400:.1f}d" if span else "open",
                         dur(span / btcmath.RETARGET_INTERVAL) if span else "–"])
        specs = [C("date", "left", DIM), C("height", style=f"bold {TEXT}", drop=3), C("difficulty"), C("change"), C("", "left", drop=1),
                 C("epoch ran", style=DIM, drop=4), C("avg block", style=DIM, drop=5)]
        return out + grid(specs, rows, w, bar_max=24)

    def hint(self) -> str:
        return "j k scroll the adjustments"

    def menu(self) -> list[tuple[str, str]]:
        return [("HASH", "HASH"), ("MINE", "MINE"), ("HASHP", "HASHP"), ("HALV", "HALV")]

    def export(self):
        return (["time", "day_utc", "height", "difficulty", "change"],
                [[int(r[0]), ui.day(float(r[0])), int(r[1]), r[2], round(float(r[3]) - 1, 6)] for r in self.past])


# ── HASHP ───────────────────────────────────────────────────

class HashpPane(PeriodPane):
    code, every = "HASHP", 300
    PERIODS, DEFAULT = REVENUE_PERIODS, "1w"

    def __init__(self, hub, security=None, args=()) -> None:
        super().__init__(hub, security, args)
        self.rewards: list[dict[str, Any]] = []
        self.fees: list[dict[str, Any]] = []
        self.stats: dict[str, Any] = {}
        self.hashrate = 0.0
        self.kept: list[Provenance] = []

    async def load(self) -> None:
        self.title = f"hashprice · {self.period}"
        self.rewards, p = await self.ask("rewards", self.period)
        self.shown = self.period
        provs = [p]
        fees, _ = await self.try_("block_fees", self.period)
        self.fees = fees or []
        stats, _ = await self.try_("reward_stats", 144)
        self.stats = stats or {}
        hr, p2 = await self.try_("hashrate", "3m")
        self.hashrate = float((hr or {}).get("currentHashrate") or 0)
        provs += await self.load_tip()
        await self.load_price()
        self.kept = provs + ([p2] if p2 else [])
        self.provs = self.sources()

    def sources(self) -> list[Provenance]:
        price = self.price()[2]
        return self.kept + self.chain_provs() + ([price] if price else [])

    def cache_key(self) -> tuple:
        return (self.period, self.hub.chain.height, int(self.hub.btc_price() or 0))

    def points(self) -> list[dict[str, float]]:
        """The window, one row a point: reward, fees, the price then, the fee share."""
        fees = {x.get("avgHeight"): float(x.get("avgFees", 0)) for x in self.fees}
        out = []
        for x in self.rewards:
            r, f = float(x.get("avgRewards", 0)), fees.get(x.get("avgHeight"))
            if r > 0:
                out.append({"t": float(x.get("timestamp", 0)), "height": float(x.get("avgHeight", 0)), "reward": r, "fees": f,
                            "usd": float(x.get("USD") or 0), "share": f / r if f is not None else None})
        return out

    def inputs(self) -> dict[str, Any]:
        """Everything the formula needs, each with where it came from."""
        pts, height = self.points(), self.tip() or int(max((x.get("avgHeight", 0) for x in self.rewards), default=0))
        subsidy = btcmath.subsidy_sats(height)
        fees = [p["fees"] for p in pts if p["fees"] is not None]
        mean_fees = mean(fees) if fees else max(mean([p["reward"] for p in pts]) - subsidy, 0.0)
        bpd, basis = blocks_per_day(self.rewards), "block times across the window"
        if bpd is None:
            avg, seen = self.spacing()
            bpd, basis = 86400.0 / avg, "this difficulty epoch's average block time" if seen else "the 144 a day target: nothing observed"
        price, label, prov = self.price()
        usd, sats = btcmath.hashprice(subsidy, mean_fees, bpd, price or 0.0, self.hashrate)
        return {"height": height, "subsidy": subsidy, "fees": mean_fees, "bpd": bpd, "basis": basis, "price": price, "price_label": label,
                "price_prov": prov, "usd": usd, "sats": sats, "points": pts}

    def draw(self, w: int, h: int) -> list[Text]:
        if not self.rewards:
            return []
        x = self.inputs()
        self.provs = self.sources()
        price, pts, per_block = x["price"], x["points"], x["subsidy"] + x["fees"]
        out = [ui.section("HASHPRICE", w, f"{self.shown} window")]
        if self.hashrate <= 0:
            out.append(ui.note("No hashrate estimate arrived, so hashprice cannot be computed. Revenue below still stands.", RED))
        else:
            in_usd = [(f"{ui.usd(x['usd'])}" if price else "no price", f"bold {ORANGE}"), (" per PH/s a day", DIM)]
            in_sats = [(f"{fmt.sats(x['sats'])}", f"bold {ORANGE}"), (" per PH/s a day", DIM)]
            per_th = (f"   ${x['usd'] / 1000:.4f} per TH/s", FAINT) if price else ""
            out += [row(w, *in_usd, ("   ", ""), *in_sats, per_th)] if w >= 70 else [row(w, *in_usd, per_th), row(w, *in_sats)]
            out += ui.wrap("(subsidy + mean fees a block) x observed blocks a day x price / hashrate", w, FAINT)
        span = pts[-1]["t"] - pts[0]["t"] if len(pts) > 1 else 0.0
        money = (lambda s: f"({ui.usd(s / 1e8 * price)})") if price else (lambda s: "")       # noqa: E731
        named = {"composite": "composite: the median of Coinbase, Kraken and Bitstamp", "": "no price source answered"}
        rows = [["subsidy", fmt.sats(x["subsidy"]), f"{x['subsidy'] / 1e8:g} BTC a block at #{x['height']:,}"],
                ["mean fees a block", fmt.sats(x["fees"]), f"{money(x['fees'])} over {len(pts)} points spanning {dur(span)}".strip()],
                ["fee share of revenue", f"{x['fees'] / per_block:.2%}" if per_block else "–", "fees over subsidy plus fees"],
                ["blocks a day", f"{x['bpd']:,.1f}", f"observed from {x['basis']}; the target is 144"],
                ["price", ui.usd(price) if price else "–", named.get(x["price_label"], x["price_label"])],
                ["hashrate", ui.hashrate(self.hashrate) if self.hashrate else "–", "mempool's current estimate"]]
        out += [Text("")] + grid([C("input", "left", DIM), C("value", style=f"bold {TEXT}"), C("from", "left", FAINT, drop=1)], rows, w)
        day = per_block * x["bpd"]
        rev = [["a block", fmt.sats(per_block), ui.usd(per_block / 1e8 * price) if price else "–"],
               ["a day", coins(day), ui.usd(day / 1e8 * price) if price else "–"],
               ["a day, fees only", coins(x["fees"] * x["bpd"]), ui.usd(x["fees"] * x["bpd"] / 1e8 * price) if price else "–"],
               ["a year at this pace", coins(day * 365.25, 0), ui.usd(day * 365.25 / 1e8 * price) if price else "–"]]
        out += [Text(""), ui.section("NETWORK REVENUE", w, "every miner together")]
        out += grid([C("", "left", DIM), C("bitcoin", style=f"bold {ORANGE}"), C("USD", style=f"bold {TEXT}")], rev, w)
        if self.stats:
            total, fee = float(self.stats.get("totalReward") or 0), float(self.stats.get("totalFee") or 0)        # strings at the source
            txs = float(self.stats.get("totalTx") or 0)
            first, last = int(self.stats.get("startBlock", 0)), int(self.stats.get("endBlock", 0))
            out += [Text(""), ui.section("LAST 144 BLOCKS", w, f"#{first:,} to #{last:,}")]
            out += grid([C("reward", style=f"bold {ORANGE}"), C("USD", style=DIM, drop=3), C("fees"), C("fee share", style=f"bold {TEXT}"),
                         C("txs", style=DIM, drop=2), C("mean fee a tx", style=DIM, drop=1)],
                        [[coins(total), ui.usd(total / 1e8 * price) if price else "–", coins(fee, 3), f"{fee / total:.2%}" if total else "–",
                          f"{txs:,.0f}", fmt.sats(fee / txs) if txs else "–"]], w)
        if len(pts) >= 2:
            out += [Text("")] + self.window(pts, x["bpd"], w, h - len(out) - 1)
        return clip(out, w)

    def window(self, pts: list[dict[str, float]], bpd: float, w: int, room: int) -> list[Text]:
        label = ui.stamp if pts[-1]["t"] - pts[0]["t"] < 3 * 86400 else ui.day
        out = [ui.section("ACROSS THE WINDOW", w, f"{label(pts[0]['t'])} to {label(pts[-1]['t'])}" if w >= 70 else "")]
        share = [p["share"] * 100 for p in pts if p["share"] is not None]
        btc_day = [p["reward"] * bpd / 1e8 for p in pts]
        usd_day = [p["reward"] * bpd / 1e8 * p["usd"] for p in pts if p["usd"]]
        sw = max(min(w - 58, 60), 8) if w >= 70 else max(w - 34, 6)
        rows = []
        series = (("fee share %", share, CYAN, lambda v: f"{v:.2f}"), ("revenue a day, BTC", btc_day, ORANGE, lambda v: f"{v:,.1f}"),
                  ("revenue a day, USD", usd_day, GREEN, ui.usd))
        for label, vals, colour, show in series:
            if vals:
                rows.append([label, Text(charts.spark(vals, sw), style=charts.snap(colour)), show(vals[-1]), show(min(vals)), show(max(vals))])
        out += grid([C("", "left", DIM, shrink=11), C("", "left"), C("last", style=f"bold {TEXT}"), C("low", style=DIM, drop=2),
                     C("high", style=DIM, drop=1)], rows, w)
        if room - len(out) >= 8 and share and w >= 60:
            xs = [p["t"] for p in pts if p["share"] is not None]
            rx, ry = charts.resample(xs, share, max(w * 2, 24))
            plot = charts.Plot(w, min(room - len(out) - 1, 14)).line(rx, ry, CYAN, label="fee share of revenue, %")
            if len(usd_day) == len(pts):
                ux, uy = charts.resample([p["t"] for p in pts], [v / 1e6 for v in usd_day], max(w * 2, 24))
                plot.line(ux, uy, GREEN, axis=1, label="revenue a day, $M")
            out += plot_lines(plot) + [plot.legend()]
        return out

    def hint(self) -> str:
        return f"[ ] window ({self.period})"

    def menu(self) -> list[tuple[str, str]]:
        return [("HASH", "HASH"), ("DIFF", "DIFF"), ("MINE", "MINE"), ("FEES", "FEES"), ("HALV", "HALV")]

    def export(self):
        return (["timestamp", "avg_height", "avg_reward_sat", "avg_fees_sat", "fee_share", "usd_price"],
                [[int(p["t"]), int(p["height"]), p["reward"], p["fees"], round(p["share"], 6) if p["share"] is not None else None, p["usd"]]
                 for p in self.points()])


# ── HALV ────────────────────────────────────────────────────

class HalvPane(MiningPane):
    code, every, tick = "HALV", 120, 1.0

    def __init__(self, hub, security=None, args=()) -> None:
        super().__init__(hub, security, args)
        self.kept: list[Provenance] = []

    async def load(self) -> None:
        self.title = "halving"
        self.kept = await self.load_tip()
        if not self.tip():
            raise SourceError("no Bitcoin backend gave a tip height")
        self.provs = self.chain_provs() or self.kept

    def cache_key(self) -> tuple:
        return (self.hub.chain.height, self.hub.chain.difficulty.get("timeAvg"))

    def draw(self, w: int, h: int) -> list[Text]:
        height, now = self.tip(), time.time()
        if not height:
            return []
        self.provs = self.chain_provs() or self.kept
        avg, seen = self.spacing()
        hv, target = btcmath.halving(height, avg), btcmath.halving(height)
        done = (height - hv.era * btcmath.HALVING_INTERVAL) / btcmath.HALVING_INTERVAL
        out = [ui.section("NEXT HALVING", w, f"#{hv.next_height:,}"),
               row(w, (f"{hv.blocks_left:,} blocks", f"bold {ORANGE}"), (f"   ~{hv.est_seconds / 86400:,.0f} days", f"bold {TEXT}"),
                   (f"   {ui.day(now + hv.est_seconds)} UTC", f"bold {TEXT}"), (f"   tip #{height:,}", FAINT))]
        at_target = f" · at the 10m target: {ui.day(now + target.est_seconds)}, ~{target.est_seconds / 86400:,.0f} days"
        out.append(row(w, ("at ", FAINT), (f"{dur(avg)} a block", DIM),
                       (", observed this epoch" if seen else ", the target: no pace observed", FAINT), (at_target, FAINT) if seen else ""))
        out.append(row(w, ui.kv("subsidy", f"{hv.subsidy_now / 1e8:g} BTC", f"bold {ORANGE}"), (" falls to ", DIM),
                       (f"{hv.subsidy_next / 1e8:g} BTC", f"bold {TEXT}"),
                       (f"   era {hv.era + 1} of {LAST_ERA + 1}", FAINT)))
        pct = f" {done:6.2%}"
        out += [ui.t(track(done, max(w - len(pct), 8)), (pct, f"bold {TEXT}")),
                ui.note(f"era {hv.era + 1}: block {hv.era * btcmath.HALVING_INTERVAL:,} to {hv.next_height - 1:,}"), Text(""),
                ui.section("ISSUANCE SCHEDULE", w, "protocol maximum issued")]
        rows = []
        for r in era_rows(height, now):
            d = datetime.fromtimestamp(r["at"], UTC)
            reached = d.strftime("%d %b %Y") if r["known"] else d.strftime("~%b %Y") if r["era"] == hv.era + 2 else d.strftime("~%Y")
            sub = f"{r['subsidy'] / 1e8:.8f}".rstrip("0").rstrip(".")
            rows.append([str(r["era"]), f"{r['start']:,}", sub, fmt.sats(r["subsidy"]), reached, f"{r['supply']:,.4f}", f"{r['share']:.6%}"])
        specs = [C("era", style=FAINT), C("starts at", style=DIM), C("subsidy BTC", style=f"bold {TEXT}"), C("in sats", style=DIM, drop=3),
                 C("reached", "left", DIM, drop=1), C("supply at era end", drop=2), C("of 21M")]
        out += grid(specs, rows, w, cursor=hv.era)
        out += ui.wrap(f"After era {LAST_ERA + 1} the subsidy is zero and miners earn fees alone. The cap is {btcmath.MAX_SUPPLY_BTC:,.4f} BTC. "
                       "Dates after today assume ten minutes a block. ~ marks an estimate.", w, FAINT)
        return clip(out, w)

    def menu(self) -> list[tuple[str, str]]:
        return [("SUPL", "SUPL"), ("HASHP", "HASHP"), ("DIFF", "DIFF"), ("BTC", "BTC")]

    def export(self):
        return (["era", "start_height", "subsidy_sats", "reached_utc", "estimated", "supply_at_era_end_btc", "share_of_21m"],
                [[r["era"], r["start"], r["subsidy"], datetime.fromtimestamp(r["at"], UTC).strftime("%Y-%m-%d"), not r["known"], r["supply"],
                  round(r["share"], 8)] for r in era_rows(self.tip(), time.time())])


# ── SUPL ────────────────────────────────────────────────────

class SuplPane(MiningPane):
    code, every = "SUPL", 600

    def __init__(self, hub, security=None, args=()) -> None:
        super().__init__(hub, security, args)
        self.supply: SeriesData | None = None
        self.utxos: SeriesData | None = None
        self.kept: list[Provenance] = []

    async def load(self) -> None:
        self.title = "supply"
        provs = await self.load_tip()
        await self.load_price()
        metrics = bitview.onchain_catalogue().get("metrics", {})
        if bv := self.hub.sources.get("bitview"):
            for attr, key in (("supply", "circulating_supply"), ("utxos", "utxo_count")):
                name = (metrics.get(key) or {}).get("series")            # only names the catalogue checked: never a guess
                if not name:
                    continue
                try:
                    s = await bv.series(name, "day1", start=-60)
                    setattr(self, attr, s)
                    provs.append(s.prov)
                except (SourceError, AttributeError):
                    pass
        if not self.tip() and not self.supply:
            raise SourceError("no tip height and no supply series: nothing to show")
        self.kept = provs
        self.provs = self.sources()

    def sources(self) -> list[Provenance]:
        price = self.price()[2]
        return self.chain_provs() + self.kept + ([price] if price else [])

    def cache_key(self) -> tuple:
        return (self.hub.chain.height, int(self.hub.btc_price() or 0))

    def draw(self, w: int, h: int) -> list[Text]:
        height = self.tip()
        price = self.price()[0]
        self.provs = self.sources()
        out: list[Text] = []
        if height:
            issued = btcmath.issued_sats(height) / 1e8
            out += [ui.section("SUPPLY", w, f"computed at #{height:,}"),
                    row(w, (f"{issued:,.2f} BTC", f"bold {ORANGE}"), ("  protocol maximum issued", DIM),
                        (f"  ({ui.usd(issued * price)})", DIM) if price else "")]
            out += ui.wrap("From the tip height and the halving schedule. Some coins are provably unspendable, so less can move.", w, FAINT)
            pct = f" {issued / 21e6:7.3%}"
            out.append(ui.t(track(issued / 21e6, max(w - len(pct) - 7, 8)), (pct, f"bold {TEXT}"), (" of 21M", FAINT)))
            out.append(row(w, ui.kv("left to mine", f"{btcmath.MAX_SUPPLY_BTC - issued:,.2f} BTC"),
                           (f"   cap {btcmath.MAX_SUPPLY_BTC:,.4f} BTC", FAINT)))
        if self.supply and self.supply.last is not None:
            s = self.supply
            out.append(row(w, ui.kv(f"bitview {s.name}", f"{s.last:,.2f} BTC"), (f"  daily · {ui.day(s.times[-1])}, still forming", DIM)))
            if height:
                out.append(row(w, ui.kv("gap to the protocol maximum", Text("")), ui.signed(s.last - issued, 2), (" BTC", DIM),
                               ("  unclaimed subsidy, and the lag of a daily series", FAINT)))
        if height:
            out += [Text("")] + self.issuance(height, price, w)
        if self.utxos and self.utxos.last is not None:
            out += [Text("")] + self.utxo_set(w)
        return clip(out, w)

    def issuance(self, height: int, price: float | None, w: int) -> list[Text]:
        avg, seen = self.spacing()
        sub, bpd = btcmath.subsidy_sats(height) / 1e8, 86400.0 / avg
        hv = btcmath.halving(height)
        money = (lambda b: ui.usd(b * price)) if price else (lambda b: "–")           # noqa: E731
        rows = [["observed pace" if seen else "none observed", f"{bpd:,.1f}", f"{sub * bpd:,.1f} BTC", money(sub * bpd),
                 f"{btcmath.annual_inflation(height, bpd):.3%}"],
                ["at the target", "144.0", f"{sub * 144:,.1f} BTC", money(sub * 144), f"{btcmath.annual_inflation(height):.3%}"],
                ["after halving", "144.0", f"{hv.subsidy_next / 1e8 * 144:,.1f} BTC", money(hv.subsidy_next / 1e8 * 144),
                 f"{btcmath.annual_inflation(hv.next_height):.3%}"]]
        out = [ui.section("ISSUANCE", w, f"subsidy {sub:g} BTC a block")]
        out += grid([C("pace", "left", DIM), C("blocks a day", drop=1), C("issued a day", style=f"bold {ORANGE}"),
                     C("USD a day", style=DIM, drop=2), C("annual infl.", style=f"bold {TEXT}")], rows, w)
        out += ui.wrap(f"Observed is this difficulty epoch's average of {dur(avg)} a block. The halving is at #{hv.next_height:,}. Annual "
                       "inflation is a year of issuance at that pace over what has been issued.", w, FAINT)
        s = self.supply
        if s and len(s.values) >= 3 and s.values[-2] is not None and s.values[-3] is not None:
            out.append(row(w, ui.kv("bitview: issued on", ui.day(s.times[-2])), (f"  {s.values[-2] - s.values[-3]:,.2f} BTC", f"bold {TEXT}"),
                           ("  daily · the last complete day", DIM)))
        return out

    def utxo_set(self, w: int) -> list[Text]:
        u = self.utxos
        vals = [v for v in u.values if v is not None]
        day = u.times[-2] if len(u.values) > 1 else u.times[-1]
        settled = u.settled() or u.last
        out = [ui.section("UTXO SET", w, f"bitview {u.name} · daily"),
               row(w, (f"{settled:,.0f}", f"bold {ORANGE}"), (" unspent outputs", DIM), (f"  daily · {ui.day(day)}", DIM),
                   (f"  {len(vals)}-day change ", FAINT), ui.signed_pct(vals[-1] / vals[0] - 1 if vals[0] else None, 2))]
        out.append(row(w, (f"{len(vals)} days  ", FAINT), (charts.spark(vals, max(w - 30, 8)), CYAN),
                       (f"  {ui.big(min(vals))} to {ui.big(max(vals))}", FAINT)))
        if self.supply:
            sv = [v for v in self.supply.values if v is not None]
            out.append(row(w, ("supply   ", FAINT), (charts.spark(sv, max(w - 30, 8)), ORANGE), (f"  +{sv[-1] - sv[0]:,.0f} BTC, daily", FAINT)))
        return out

    def menu(self) -> list[tuple[str, str]]:
        return [("HALV", "HALV"), ("HASHP", "HASHP"), ("ONCH", "ONCH"), ("BTC", "BTC")]

    def export(self):
        s, u = self.supply, self.utxos
        if not s and not u:
            return None
        base = s or u
        by_t = {t: v for t, v in zip(u.times, u.values, strict=True)} if u else {}
        return (["day_utc", "circulating_supply_btc", "utxo_count"],
                [[ui.day(t), (s.values[i] if s else None), by_t.get(t)] for i, t in enumerate(base.times)])


# ── LN ──────────────────────────────────────────────────────

def ln_as_of(latest: dict[str, Any]) -> float:
    try:
        return datetime.fromisoformat(str(latest.get("added", "")).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


class LnPane(PeriodPane):
    code, every = "LN", 3600
    PERIODS, DEFAULT = LN_PERIODS, "3m"

    def __init__(self, hub, security=None, args=()) -> None:
        super().__init__(hub, security, args)
        self.latest: dict[str, Any] = {}
        self.stats: list[dict[str, Any]] = []
        self.countries: list[dict[str, Any]] = []
        self.isps: dict[str, Any] = {}
        self.ranks: dict[str, Any] = {}
        self.as_of = 0.0
        self.kept: list[Provenance] = []

    async def load(self) -> None:
        self.title = f"Lightning · {self.period}"
        got, p = await self.ask("ln_latest")
        self.latest = (got or {}).get("latest") or {}
        self.as_of = ln_as_of(self.latest)
        for attr, method, args, empty in (("stats", "ln_stats", (self.period,), []), ("countries", "ln_countries", (), []),
                                          ("isps", "ln_isps", (), {}), ("ranks", "ln_rankings", (), {})):
            v, _ = await self.try_(method, *args)
            setattr(self, attr, v if isinstance(v, type(empty)) else empty)
        self.shown = self.period
        await self.load_price()
        # The statistics describe `added`, not the moment they were fetched. When they are days old the frame must say
        # stale and dim the page, so the provenance carries the day the numbers describe as the time they were last good.
        old = self.as_of and time.time() - self.as_of > LN_STALE_S
        self.kept = [Provenance(p.source, self.as_of if old else p.fetched_at, self.as_of or p.fetched_at, DAILY)]
        self.provs = self.sources()

    def sources(self) -> list[Provenance]:
        """While the statistics are stale they stand alone in the frame, so `stale 21d` always has room to show."""
        price = self.price()[2]
        return self.kept + ([price] if price and not any(p.stale() for p in self.kept) else [])

    def draw(self, w: int, h: int) -> list[Text]:
        m, now = self.latest, time.time()
        if not m:
            return []
        price = self.price()[0]
        self.provs = self.sources()
        age = now - self.as_of if self.as_of else 0.0
        stale = age > LN_STALE_S
        out = [row(w, (" STALE " if stale else " AS OF ", f"bold #0D0D0D on {RED if stale else ORANGE}"),
                   (f" as of {ui.day(self.as_of)}", f"bold {TEXT}"),
                   (f" · {age / 86400:.0f} days old", f"bold {RED}" if stale else DIM),
                   (" · the source has stopped updating these statistics" if stale else " · daily statistics", FAINT))]
        cap, nodes = float(m.get("total_capacity", 0)), int(m.get("node_count", 0))
        out.append(row(w, ui.kv("capacity", coins(cap), f"bold {ORANGE}"), (f" ({ui.usd(cap / 1e8 * price)})", DIM) if price else "",
                       ui.kv("   channels", f"{int(m.get('channel_count', 0)):,}"), ui.kv("   nodes", f"{nodes:,}")))
        if m.get("tor_nodes") is not None and nodes:
            split = [("tor", "tor_nodes"), ("clearnet", "clearnet_nodes"), ("both", "clearnet_tor_nodes"), ("unannounced", "unannounced_nodes")]
            parts = [Text.assemble((f"{label} ", FAINT), (f"{int(m[k]):,}", f"bold {TEXT}"), (f" {int(m[k]) / nodes:.0%}   ", DIM))
                     for label, k in split if m.get(k) is not None]
            out.append(row(w, ("nodes  ", FAINT), *parts))
        out.append(row(w, ui.kv("channel size", fmt.sats(float(m.get("avg_capacity", 0)))), (" mean", FAINT),
                       (f" ({ui.usd(float(m.get('avg_capacity', 0)) / 1e8 * price)})", DIM) if price else "",
                       ui.kv("   median", fmt.sats(float(m.get("med_capacity", 0))))))
        out.append(row(w, ui.kv("fee rate", f"{int(m.get('avg_fee_rate', 0)):,} ppm"), (" mean", FAINT),
                       ui.kv("   median", f"{int(m.get('med_fee_rate', 0)):,} ppm"),
                       ui.kv("   base fee", f"{float(m.get('avg_base_fee_mtokens', 0)) / 1000:g} sat"), (" mean", FAINT),
                       ui.kv("   median", f"{float(m.get('med_base_fee_mtokens', 0)) / 1000:g} sat")))
        hist = sorted(self.stats, key=lambda x: float(x.get("added", 0)))
        ph = min(h - len(out) - 2, 16)
        if len(hist) >= 2 and ph >= 4:
            xs = [float(x["added"]) for x in hist]
            rx, rc = charts.resample(xs, [float(x.get("total_capacity", 0)) / 1e8 for x in hist], max(w * 2, 24))
            _, rn = charts.resample(xs, [float(x.get("channel_count", 0)) for x in hist], max(w * 2, 24))
            plot = charts.Plot(w, ph).line(rx, rc, ORANGE, label="capacity, BTC").line(rx, rn, CYAN, axis=1, label="channels")
            out += [Text("")] + plot_lines(plot) + [plot.legend()]
        elif len(hist) >= 2:
            out.append(row(w, (f"capacity {self.shown}  ", FAINT),
                           (charts.spark([float(x.get("total_capacity", 0)) for x in hist], max(w - 16, 8)), ORANGE)))
        lw = (w - 3) // 2
        geo, isp = self.by_country(lw if w >= 100 else w), self.by_isp(lw if w >= 100 else w)
        if geo or isp:
            out += [Text("")] + (beside(geo, isp, lw) if w >= 100 and geo and isp else geo + ([Text("")] if geo and isp else []) + isp)
        big_, busy = self.top_capacity(lw if w >= 100 else w, price), self.top_channels(lw if w >= 100 else w)
        if big_ or busy:
            out += [Text("")] + (beside(big_, busy, lw) if w >= 100 and big_ and busy else big_ + ([Text("")] if big_ and busy else []) + busy)
        return clip(out, w)

    def by_country(self, w: int) -> list[Text]:
        rows_ = self.countries[:10]
        if not rows_:
            return []
        top = max(float(c.get("share", 0)) for c in rows_) or 1.0
        rows = [[str(c.get("iso", "")), clean((c.get("name") or {}).get("en")), f"{int(c.get('count', 0)):,}",
                 f"{float(c.get('share', 0)):.1f}%",
                 Bar(float(c.get("share", 0)) / top, CYAN), f"{float(c.get('capacity') or 0) / 1e8:,.1f}"] for c in rows_]
        return [ui.section("NODES BY COUNTRY", w, "top 10")] + grid(
            [C("", "left", FAINT, drop=4), C("country", "left", shrink=9), C("nodes"), C("share", style=f"bold {TEXT}"), C("", "left", drop=1),
             C("BTC", style=DIM, drop=2)], rows, w, bar_max=20)

    def by_isp(self, w: int) -> list[Text]:
        ranking = sorted((r for r in self.isps.get("ispRanking") or [] if len(r) >= 5), key=lambda r: -float(r[2]))[:10]
        if not ranking:
            return []
        total = sum(float(self.isps.get(k) or 0) for k in ("clearnetCapacity", "torCapacity", "unknownCapacity"))
        total = total or sum(float(r[2]) for r in ranking)
        top = float(ranking[0][2]) or 1.0
        rows = [[clean(r[1]), f"{float(r[2]) / 1e8:,.1f}", f"{float(r[2]) / total:.1%}", Bar(float(r[2]) / top), f"{int(r[4]):,}",
                 f"{int(r[3]):,}"]
                for r in ranking]
        out = [ui.section("CAPACITY BY ISP", w, "top 10")] + grid(
            [C("ISP", "left", shrink=9), C("BTC"), C("share", style=f"bold {TEXT}"), C("", "left", drop=1), C("nodes", style=DIM, drop=2),
             C("chans", style=DIM, drop=3)], rows, w, bar_max=20)
        if self.isps.get("torCapacity") is not None:
            out.append(row(w, ui.kv("tor only", coins(float(self.isps.get("torCapacity") or 0), 1), DIM),
                           ui.kv("   unknown", coins(float(self.isps.get("unknownCapacity") or 0), 1), DIM)))
        return out

    def top_capacity(self, w: int, price: float | None) -> list[Text]:
        nodes = (self.ranks.get("topByCapacity") or [])[:10]
        if not nodes:
            return []
        total, top = float(self.latest.get("total_capacity") or 0), max(float(n.get("capacity", 0)) for n in nodes) or 1.0
        rows = [[str(i + 1), clean(n.get("alias") or str(n.get("publicKey", ""))[:12]), f"{float(n.get('capacity', 0)) / 1e8:,.1f}",
                 ui.usd(float(n.get("capacity", 0)) / 1e8 * price) if price else "–",
                 f"{float(n.get('capacity', 0)) / total:.1%}" if total else "–", Bar(float(n.get("capacity", 0)) / top)]
                for i, n in enumerate(nodes)]
        return [ui.section("TOP NODES BY CAPACITY", w)] + grid(
            [C("#", style=FAINT), C("alias", "left", f"bold {TEXT}", shrink=10), C("BTC", style=f"bold {ORANGE}"), C("USD", style=DIM, drop=1),
             C("of all", style=DIM, drop=2), C("", "left", drop=3)], rows, w, bar_max=20)

    def top_channels(self, w: int) -> list[Text]:
        nodes = (self.ranks.get("topByChannels") or [])[:10]
        if not nodes:
            return []
        top = max(float(n.get("channels", 0)) for n in nodes) or 1.0
        rows = [[str(i + 1), clean(n.get("alias") or str(n.get("publicKey", ""))[:12]), f"{int(n.get('channels', 0)):,}",
                 str(n.get("iso_code") or "–"), Bar(float(n.get("channels", 0)) / top, CYAN)]
                for i, n in enumerate(nodes)]
        return [ui.section("TOP NODES BY CHANNELS", w)] + grid(
            [C("#", style=FAINT), C("alias", "left", f"bold {TEXT}", shrink=10), C("channels", style=f"bold {CYAN}"),
             C("where", style=DIM, drop=1), C("", "left", drop=2)], rows, w, bar_max=20)

    def hint(self) -> str:
        return f"[ ] window ({self.period}) · j k scroll"

    def menu(self) -> list[tuple[str, str]]:
        return [("BTC", "BTC"), ("MEMP", "MEMP"), ("FEES", "FEES")]

    def export(self):
        return (["day_utc", "total_capacity_sat", "channel_count", "tor_nodes", "clearnet_nodes", "unannounced_nodes", "clearnet_tor_nodes"],
                [[ui.day(float(x.get("added", 0))), x.get("total_capacity"), x.get("channel_count"), x.get("tor_nodes"), x.get("clearnet_nodes"),
                  x.get("unannounced_nodes"), x.get("clearnet_tor_nodes")] for x in sorted(self.stats, key=lambda x: float(x.get("added", 0)))])


# ── ORCL ────────────────────────────────────────────────────

ORACLE_WORDS = ("People pay each other round dollar amounts, so the bitcoin amounts of on-chain payments pile up wherever $10, $20, $50 or $100 "
                "lands at the going price. Bitview finds those piles in recent blocks and reads the price off their position: no exchange is "
                "involved.")


def marker_lines(marks: Sequence[tuple[int, str]], width: int) -> list[Text]:
    """A row of carets under the chart and a row of labels under the carets, both cyan. Labels that would touch are left out."""
    carets, labels = [" "] * width, [" "] * width
    for col, _ in marks:
        if 0 <= col < width:
            carets[col] = "▲"
    order = sorted(marks, key=lambda m: (0 if m[1] in ("$100", "$10", "$1k") else 1, m[0]))
    for col, label in order:
        at = min(max(col - len(label) // 2, 0), width - len(label))
        if 0 <= col < width and at >= 0 and all(ch == " " for ch in labels[max(at - 1, 0):at + len(label) + 1]):
            labels[at:at + len(label)] = label
    return [Text("".join(carets).rstrip(), style=CYAN, no_wrap=True), Text("".join(labels).rstrip(), style=f"bold {CYAN}", no_wrap=True)]


class OrclPane(MiningPane):
    code, every = "ORCL", 60

    def __init__(self, hub, security=None, args=()) -> None:
        super().__init__(hub, security, args)
        self.oracle = 0.0
        self.hists: dict[str, list[int]] = {}
        self.kind = "outputs" if any(a.lower() == "outputs" for a in self.args) else "payments"
        self.kept: list[Provenance] = []

    async def load(self) -> None:
        self.title = "on-chain price"
        bv = self.hub.sources.get("bitview")
        if bv is None:
            raise SourceError("the oracle needs a Bitview backend (SET bitcoin.bitview)")
        self.oracle, p = await bv.oracle_price()
        provs = [p]
        for kind in ("payments", "outputs"):
            try:
                self.hists[kind], _ = await bv.oracle_histogram(kind)
            except SourceError:
                self.hists.pop(kind, None)
        await self.load_price()
        self.kept = provs
        self.provs = self.sources()

    def sources(self) -> list[Provenance]:
        price = self.price()[2]
        return self.kept + ([price] if price else [])

    def cache_key(self) -> tuple:
        return (self.kind, int(self.hub.btc_price() or 0))

    def draw(self, w: int, h: int) -> list[Text]:
        if not self.oracle:
            return []
        price, label, _ = self.price()
        self.provs = self.sources()
        a = [Text(f"{ui.px(self.oracle, 2)}", style=f"bold {CYAN}"), Text("  chain oracle", style=DIM)]
        b, c = [], []
        if price:
            named = "  exchange composite" if label == "composite" else f"  {label}, no composite yet"
            b = [Text(ui.px(price, 2), style=f"bold {ORANGE}"), Text(named, style=DIM)]
            c = [Text("gap ", style=FAINT), ui.signed_pct(self.oracle / price - 1, 2), Text("  "), ui.signed(self.oracle - price, 2),
                 Text(" USD", style=FAINT)]
        out = [ui.section("ON-CHAIN PRICE", w, "bitview oracle · live")]
        out += [row(w, *a, Text("     "), *b, Text("     "), *c)] if w >= 96 else [row(w, *a)] + ([row(w, *b), row(w, *c)] if price else [])
        words = ui.wrap(ORACLE_WORDS, w, DIM)
        if h >= 22:
            out += words
        hist = self.hists.get(self.kind) or []
        if hist:
            out += [Text("")] + self.chart(hist, w, h - len(out) - 1)
        if h < 22:
            out += [Text("")] + words
        if hist:
            out += [Text("")] + self.spikes(hist, w)
        return clip(out, w)

    def chart(self, hist: list[int], w: int, room: int) -> list[Text]:
        cols, lo, per = oracle_columns(hist, self.oracle, w)
        if not cols:
            return []
        marks = [((bitview.oracle_bin(usd, self.oracle) - lo) // per, dollar_label(usd)) for usd in ROUND_DOLLARS]
        at = {c for c, _ in marks}
        colours = [CYAN if i in at else ORANGE for i in range(len(cols))]
        out = [ui.section(f"{self.kind.upper()} HISTOGRAM", w, "$1 to $10k at the oracle price")]
        out += charts.vbars(cols, min(max(room - 4, 3), 12), colours=colours) + marker_lines(marks, len(cols))
        out += ui.wrap(f"log scale of amount · {per} bin{'s' if per > 1 else ''} a column, tallest shown · peak {max(cols):,.0f} · "
                       f"cyan: where round dollars land at {ui.px(self.oracle, 0)}", w, FAINT)
        if self.kind == "outputs":
            out.append(ui.note("Every output, change and dust included, so the round amounts are harder to see. o returns to payments."))
        return out

    def spikes(self, hist: list[int], w: int) -> list[Text]:
        rows = []
        for usd in ROUND_DOLLARS:
            b = bitview.oracle_bin(usd, self.oracle)
            count, ratio = (hist[b] if 0 <= b < len(hist) else 0), spike_ratio(hist, b)
            rows.append([Text(f"${usd:,}", style=f"bold {CYAN}"), str(b), fmt.sats(usd * 1e8 / self.oracle), f"{count:,}",
                         f"{ratio:.1f}x" if ratio else "–", Bar(min((ratio or 0) / 8, 1.0), CYAN)])
        return [ui.section("ROUND AMOUNTS", w, f"{self.kind} · bin = round(log10(sats) x 200)")] + grid(
            [C("amount"), C("bin", style=DIM, drop=2), C("is worth"), C("count", style=f"bold {TEXT}"), C("over nearby", style=DIM, drop=3),
             C("", "left", drop=1)], rows, w, bar_max=24)

    def key(self, k: str, ch: str | None) -> bool:
        if ch in ("o", "O"):
            self.kind = "outputs" if self.kind == "payments" else "payments"
            return True
        return False

    def command(self) -> str:
        return "ORCL outputs" if self.kind == "outputs" else "ORCL"

    def hint(self) -> str:
        return f"o {'payments' if self.kind == 'outputs' else 'outputs'} histogram"

    def menu(self) -> list[tuple[str, str]]:
        return [("BTC", "BTC"), ("GP", "GP BTC"), ("ONCH", "ONCH")]

    def export(self):
        if not self.oracle or not self.hists:
            return None
        pay, outs = self.hists.get("payments") or [], self.hists.get("outputs") or []
        lo, hi = bitview.oracle_bin(1, self.oracle), bitview.oracle_bin(10_000, self.oracle)
        return (["bin", "sats", "usd_at_oracle", "payments", "outputs"],
                [[b, round(10 ** (b / 200)), round(10 ** (b / 200) / 1e8 * self.oracle, 2), pay[b] if b < len(pay) else None,
                  outs[b] if b < len(outs) else None] for b in range(max(lo, 0), hi + 1)])


# ── the registry ────────────────────────────────────────────

_DOWN = ("When a backend is down the next one in bitcoin.order answers and the frame names the one that did. With none, the last "
         "picture stays on screen, dimmed, with its age.")
register(Function(
    "MINE", "Mining pools", "Bitcoin", "pools by share over 24 hours to 3 years; one pool's recent blocks", MinePane,
    args="[24h 3d 1w 1m 3m 6m 1y 2y 3y | pool]", needs=("pools",),
    help="Every pool that found a block in the window: rank, blocks, share, estimated hashrate, empty blocks, audit health and "
         "fee delta. The source sends no share, so share is the pool's blocks over all blocks in the window. Estimated hashrate is "
         "that share of the network estimate sent with the list: the 24 hour estimate for 24h, the three day one for 3d, the "
         "week's for anything longer. Health is the mean audit match rate: how closely the pool's blocks matched the template "
         "mempool expected. Fee delta is the mean gap between the fees a pool collected and the fees expected. [ and ] change the "
         "window and an argument sets it, as in MINE 1m. Enter opens one pool, as MINE foundryusa does: its blocks and share over 24 "
         "hours, a week and all time, its estimated hashrate, its weekly hashrate and share, and its recent blocks with age, "
         "transactions, fees, reward and match rate. Enter on a block opens BLK. Data: /api/v1/mining/pools/{period}, "
         "/mining/pool/{slug}, /pool/{slug}/blocks and /pool/{slug}/hashrate, refreshed every 10 minutes. " + _DOWN))
register(Function(
    "HASH", "Hashrate and difficulty", "Bitcoin", "network hashrate and difficulty history on one chart", HashPane,
    args="[3m 6m 1y 2y 3y all]", needs=("hashrate",),
    help="Network hashrate in EH/s (orange, right axis) and difficulty in trillions (cyan steps, left axis) over 3 months to all "
         "of history. [ and ] change the window and L switches to a log scale. The points are mempool's daily hashrate "
         "estimates, which are noisy because blocks arrive at random. So the 7 day and 30 day changes compare the mean of the "
         "last seven days with the mean of the seven days that ended 7 or 30 days earlier. The high and low are the extremes "
         "of the window on screen, and the all-time high when the window is all. Current hashrate and difficulty are the source's "
         "currentHashrate and currentDifficulty. The Bitview line is a second daily estimate from its hash_rate series, named "
         "with the day it describes. Data: /api/v1/mining/hashrate/{period}, every 30 minutes, kept on disk. " + _DOWN))
register(Function(
    "DIFF", "Difficulty adjustment", "Bitcoin", "this epoch: progress, ETA, estimated change; past retargets", DiffPane, needs=("difficulty",),
    help="The current difficulty epoch: a progress bar, blocks done and left of 2,016, the average block time this epoch, when the "
         "epoch began, the estimated change and the previous one, the estimated time of the retarget in UTC, and how far the chain "
         "runs ahead of or behind the ten minute schedule. The estimate is mempool's, over the WebSocket when it is up and from "
         "/api/v1/difficulty-adjustment every 60 s when it is not. The source mixes milliseconds and seconds and the page converts "
         "both. Below, a year of retargets from /api/v1/mining/difficulty-adjustments/1y: a bar chart, green up and red down, "
         "then date, height, difficulty, change, how long the following epoch ran and its average block time. One exception to "
         "the usual fallback: Bitview's estimated change has read far from mempool's at the same moment (-24.9% against -6.0%), so "
         "when only Bitview answers the page says UNVERIFIED in red and never shows its estimate as the number. "
         "With no backend at all the last picture stays, dimmed, with its age."))
register(Function(
    "HASHP", "Hashprice", "Bitcoin", "hashprice and miner revenue: subsidy, fees, fee share", HashpPane, args="[24h 3d 1w 1m 3m]",
    needs=("rewards", "hashrate", "quote"),
    help="Hashprice is what one PH/s of hashrate earns in a day: (subsidy + mean fees a block) x observed blocks a day x price / "
         "hashrate, in dollars and in sats. The subsidy comes from the tip height and the halving schedule. Mean fees a block is the "
         "mean of avgFees over the window from /api/v1/mining/blocks/fees/{period}. Blocks a day is observed, not the 144 target: "
         "the heights and block times of the first and last points of /mining/blocks/rewards/{period}. When the window is too "
         "short for that, the page uses this difficulty epoch's average block time and says so. The price is the composite BTC "
         "quote, or mempool's /api/v1/prices when no quote has arrived, named either way. Hashrate is currentHashrate from "
         "/mining/hashrate/3m. Network revenue is every miner together, a block, a day and a year at this pace. The last 144 "
         "blocks come from /mining/reward-stats/144. The sparklines and the chart show fee share and revenue a day across the "
         "window, each point priced at the dollar price the source recorded then. [ and ] change the window. Refreshed every 5 "
         "minutes. " + _DOWN))
register(Function(
    "HALV", "Halving", "Bitcoin", "countdown to the next halving; the full issuance schedule", HalvPane, needs=("tip",),
    help="Blocks until the subsidy next halves, the estimated days and UTC date at the block pace observed this difficulty epoch "
         "(the source's timeAvg), and the same at the ten minute target for comparison. With no pace observed the target is used "
         "and the page says so. The bar shows progress through the current 210,000 block era. The table is the whole schedule, "
         "computed locally: era, first height, subsidy in BTC and sats, when it was reached or is expected, the supply at the end "
         "of the era and its share of 21 million, with the current era highlighted. Dates of past halvings are the known ones. "
         "Future dates assume ten minutes a block from the tip and carry a ~. Nothing is fetched but the tip height and the epoch "
         "pace, which arrive over the mempool WebSocket or by one request each. " + _DOWN))
register(Function(
    "SUPL", "Supply", "Bitcoin", "supply, issuance a day, annual inflation, the UTXO set", SuplPane, needs=("tip", "series"),
    help="Supply is computed from the tip height and the halving schedule and labelled the protocol maximum issued, because some "
         "coins are provably unspendable: the genesis coinbase, unclaimed subsidies, burns. Beside it is Bitview's "
         "circulating_supply series, a daily value shown with the day it describes, and the gap between the two. Issuance a day "
         "is the subsidy times blocks a day: at the pace observed this difficulty epoch, at the 144 target, and after the next "
         "halving. Annual inflation is a year of issuance at that pace over what has been issued. Bitview's issuance for the last "
         "complete day is the difference between two daily supply values. The UTXO set is Bitview's utxo_count series: the last "
         "complete day, its 60 day change and a sparkline. Every Bitview number says daily and names its day. The series names "
         "come from data/onchain.toml and are never guessed. Refreshed every 10 minutes; the computed numbers follow the tip. When "
         "Bitview is down the computed half of the page still shows."))
register(Function(
    "LN", "Lightning network", "Bitcoin", "Lightning capacity, channels and nodes; by country and ISP; top nodes", LnPane,
    args="[1m 3m 6m 1y 2y 3y]", needs=("lightning",),
    help="The public Lightning network from mempool's statistics: capacity in BTC and dollars, channels, nodes split by tor, "
         "clearnet, both and unannounced, mean and median channel size, mean and median fee rate in parts per million and base "
         "fee in sats. The chart shows capacity (orange) and channels (cyan, left axis) over the window; [ and ] change it. Then "
         "the top 10 countries by nodes, the top 10 ISPs by capacity, and the largest nodes by capacity and by channels. The first "
         "line is the as-of date. These statistics have been seen 20 days old at the source, so when they are more than three "
         "days old the line turns red, says STALE with the age in days, and the frame dims the page. Private channels are not "
         "counted anywhere. Data: /api/v1/lightning/statistics/latest and /{period}, /nodes/countries, /nodes/isp-ranking, "
         "/nodes/rankings, every hour. " + _DOWN))
register(Function(
    "ORCL", "On-chain price oracle", "Bitcoin", "the price read from the chain alone, beside the exchanges", OrclPane, args="[outputs]",
    needs=("oracle", "quote"),
    help="Bitview's oracle price beside the exchange composite, with the gap in percent and dollars. " + ORACLE_WORDS + " The chart is "
         "the live payments histogram from /api/oracle/histogram/payments/live: 2,400 bins on a log scale, bin = round(log10(sats) "
         "x 200), drawn from $1 to $10,000 at the oracle price. Each column shows the tallest bin of its slice so a spike one bin "
         "wide survives. Cyan carets and labels mark where $10, $20, $50, $100, $200, $500 and $1,000 land at the oracle price, "
         "so the spikes should stand on the carets. The table gives each round amount's bin, its worth in sats, the count in that "
         "bin and how far it stands above the median of the bins around it. o switches to the outputs histogram, which counts "
         "every output and buries the spikes under change and dust. The composite is the median of Coinbase, Kraken and Bitstamp; "
         "before it arrives mempool's price stands in and is named. Refreshed every 60 s. The oracle is Bitview's alone: when "
         "Bitview is down the last picture stays, dimmed, with its age."))
