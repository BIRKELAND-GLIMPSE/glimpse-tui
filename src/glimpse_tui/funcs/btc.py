"""BTC, MEMP and FEES: the Bitcoin page, the mempool as it fills, and what a transaction costs right now.

This module is also the pattern for every function page. A pane loads in `load` (through the hub, off the paint
path), keeps what it got on `self`, names every source in `self.provs`, and draws from that alone in `draw`.
"""
from __future__ import annotations

import time

from rich.text import Text

from .. import charts, fmt
from ..data import btcmath
from ..data.core import Provenance, SourceError
from ..term import ui
from ..term.panes import FuncPane
from ..term.registry import Function, register
from ..theme import DIM, FAINT, ORANGE, TEXT

BLOCK_VB = 1_000_000.0


def fee_text(rate: float) -> str:
    return f"{rate:.0f}" if rate >= 10 or rate == int(rate) else f"{rate:.1f}" if rate >= 1 else f"{rate:.2f}"


def fee_span(fee_range: list[float]) -> str:
    if not fee_range:
        return "–"
    lo, hi = fee_text(fee_range[0]), fee_text(fee_range[-1])
    return lo if lo == hi else f"{lo}-{hi}"


def short_count(n: float) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else f"{n:.0f}"


def block_picture(txs: list, width: int, rows: int) -> list[Text]:
    """The next block, drawn: every transaction in template order (highest fee rate first), each half-block pixel a
    fixed slice of the block's million virtual bytes, coloured by fee rate. Unfilled space at the end is block space
    nobody has bid for yet."""
    n = width * rows * 2
    if n <= 0:
        return []
    slice_vb, flat, cum = BLOCK_VB / n, [None] * n, 0.0
    for tx in sorted(txs, key=lambda t: -t.rate):
        a, cum = int(cum / slice_vb), cum + tx.vsize
        b = min(max(int(cum / slice_vb), a + 1), n)
        colour = charts.fee_colour(tx.rate)
        for i in range(a, b):
            flat[i] = colour
        if b >= n:
            break
    return charts.pixels([flat[r * width:(r + 1) * width] for r in range(rows * 2)])


def bands_picture(fee_range: list[float], filled: float, width: int, rows: int) -> list[Text]:
    """Without the transaction stream the same picture is drawn from the block's seven fee percentiles."""
    n = width * rows * 2
    if n <= 0 or not fee_range:
        return []
    top = list(reversed(fee_range))
    flat = [None] * n
    for i in range(int(n * min(filled, 1.0))):
        flat[i] = charts.fee_colour(top[min(int(i / (n * filled) * len(top)), len(top) - 1)])
    return charts.pixels([flat[r * width:(r + 1) * width] for r in range(rows * 2)])


def fee_legend(width: int) -> Text:
    out = Text(no_wrap=True)
    out.append("1 ", style=FAINT)
    steps = max(min(width - 16, 28), 6)
    for i in range(steps):
        out.append("▀", style=charts.snap(charts.ramp(charts.FEE_STOPS, i / (steps - 1))))
    out.append(" 500 sat/vB", style=FAINT)
    return out


class MempPane(FuncPane):
    code, every, tick = "MEMP", 30, 1.0

    def visibility_changed(self, on: bool) -> None:
        self.hub.track("block", on)                 # the next block's transactions stream only while this page is on screen

    async def load(self) -> None:
        self.title = "mempool"
        c, hub = self.hub.chain, self.hub
        last: SourceError | None = None
        for key in hub.bitcoin_order():             # the first healthy backend in the user's order
            src = hub.sources[key]
            try:
                info, prov = await src.mempool()
                self.histogram = info.get("fee_histogram", [])
                if not c.streaming:
                    c.mempool_blocks, _ = await src.mempool_blocks()
                    c.fees, _ = await src.fees()
                    c.mempool_count, c.mempool_vsize = int(info.get("count", 0)), float(info.get("vsize", 0))
                self.rest_prov = prov
                return
            except (SourceError, AttributeError) as e:
                last = e if isinstance(e, SourceError) else last
        if not c.mempool_blocks:
            raise last or SourceError("no Bitcoin backend answered")

    def cache_key(self) -> tuple:
        c = self.hub.chain
        return (self.hub.projected_version, c.height, len(c.mempool_blocks), int(c.mempool_vsize // 1e5), c.streaming)

    def draw(self, w: int, h: int) -> list[Text]:
        c, now = self.hub.chain, time.time()
        self.provs = [p for p in (c.prov if c.streaming else getattr(self, "rest_prov", None),) if p]
        if self.provs and c.streaming:
            self.provs = [Provenance(f"{self.provs[0].source} ws", self.provs[0].fetched_at, self.provs[0].as_of, "live")]
        blocks = c.mempool_blocks
        if not blocks:
            return []
        self._narrow = w < 46
        bar_w = max(w - (20 if self._narrow else 35), 4)
        spacing = (c.difficulty.get("timeAvg") or 600_000) / 60_000            # minutes a block, observed this epoch
        head = f"{'fee s/vB':>9}{'eta':>6}" if self._narrow else f"{'fee s/vB':>9}{'txs':>8}{'eta':>9}"
        out = [ui.t((f"{'NEXT':<{bar_w + 5}}" if self._narrow else f"{'NEXT BLOCKS':<{bar_w + 5}}", f"bold {FAINT}"), (head, FAINT))]
        shown = blocks[:3]
        for i, b in enumerate(shown):
            out.append(self._row(f"#{i + 1}", b.get("blockVSize", 0) / BLOCK_VB, b.get("medianFee", 1), fee_span(b.get("feeRange", [])),
                                 short_count(b.get("nTx", 0)), f"~{(i + 1) * spacing:.0f}m", bar_w))
        rest = blocks[3:]
        if rest:
            vs = sum(b.get("blockVSize", 0) for b in rest)
            rng = [min(b["feeRange"][0] for b in rest if b.get("feeRange")), max(b["feeRange"][-1] for b in rest if b.get("feeRange"))]
            eta = (len(blocks)) * spacing
            out.append(self._row(f"+{max(round(vs / BLOCK_VB), 1)}", min(vs / BLOCK_VB / 8, 1.0), rest[0].get("medianFee", 1), fee_span(rng),
                                 short_count(sum(b.get("nTx", 0) for b in rest)), f"{eta / 60:.0f}h+" if eta >= 90 else f"~{eta:.0f}m", bar_w))
        tight = h < 22                                          # a small pane gives its rows to the picture
        summary = self._summary(w, now, tight)
        room = h - len(out) - len(summary) - (2 if tight else 4)
        if room >= 2:
            txs, first = list(self.hub.projected.values()), blocks[0]
            if not tight:
                out.append(Text(""))
            head = ui.t(("NEXT BLOCK ", f"bold {ORANGE}"), (f"{first.get('nTx', 0):,} tx · {first.get('blockVSize', 0) / 1000:,.0f} kvB · "
                        f"{first.get('totalFees', 0) / 1e8:.3f} BTC fees", DIM))
            out.append(head)
            rows = min(room, 14)
            filled = first.get("blockVSize", 0) / BLOCK_VB
            out += block_picture(txs, w, rows) if txs else bands_picture(first.get("feeRange", []), filled, w, rows)
            out.append(ui.fit([fee_legend(w), ("   every tx, highest fee first" if txs else "   fee bands", FAINT),
                               ("" if txs else " · the stream draws every tx", FAINT)], w))
        if not tight:
            out.append(Text(""))
        return out + summary

    def _row(self, tag: str, frac: float, median: float, fees: str, txs: str, eta: str, bar_w: int) -> Text:
        bar = charts.hbar(frac, bar_w)
        if self._narrow:                                    # under 46 columns: the bar, the fee range and the wait
            return ui.t((f"{tag:<4}", f"bold {TEXT}"), (bar, charts.snap(charts.fee_colour(median))), (" " * (bar_w - len(bar) + 1), ""),
                        (f"{fees:>9}", f"bold {TEXT}"), (f"{eta:>6}", DIM))
        return ui.t((f"{tag:<4}", f"bold {TEXT}"), (bar, charts.snap(charts.fee_colour(median))), (" " * (bar_w - len(bar) + 1), ""),
                    (f"{fees:>9}", f"bold {TEXT}"), (f"{txs:>8}", DIM), (f"{eta:>9}", DIM))

    def _summary(self, w: int, now: float, tight: bool = False) -> list[Text]:
        c, f, da = self.hub.chain, self.hub.chain.fees, self.hub.chain.difficulty
        if tight:
            chg = float(da.get("difficultyChange", 0)) / 100 if da else None
            return [ui.fit([("FEES ", f"bold {FAINT}"), *[ui.t((f"{n} ", FAINT), (f"{fee_text(float(f.get(k, 0)))} ", f"bold {TEXT}"))
                                                          for k, n in (("fastestFee", "next"), ("halfHourFee", "30m"), ("hourFee", "1h"))],
                            (f" POOL {c.mempool_vsize / 1e6:,.0f} MvB", DIM)], w),
                    ui.t(("TIP  ", f"bold {FAINT}"), (f"#{c.height:,}", f"bold {ORANGE}"),
                         (f" {ui.when(c.tip_time, now)} {c.tip_pool[:12]}", DIM),
                         ("  DIFF ", f"bold {FAINT}"), ui.signed_pct(chg) if chg is not None else "")]
        out = [ui.t(("FEES  ", f"bold {FAINT}"), *[x for k, n in (("fastestFee", "next"), ("halfHourFee", "30m"), ("hourFee", "1h"),
               ("economyFee", "eco")) for x in ((f"{n} ", FAINT), (f"{fee_text(float(f.get(k, 0)))}  ", f"bold {TEXT}"))], ("sat/vB", FAINT)),
               ui.t(("POOL  ", f"bold {FAINT}"), (f"{c.mempool_vsize / 1e6:,.0f} MvB", f"bold {TEXT}"), (f"  {c.mempool_count:,} tx", DIM),
                    (f"  min {fee_text(float(f.get('minimumFee', 0)))}", DIM))]
        if c.height:
            out.append(ui.t(("TIP   ", f"bold {FAINT}"), (f"#{c.height:,}", f"bold {ORANGE}"), (f"  {ui.when(c.tip_time, now)}  ", DIM),
                            (c.tip_pool, f"bold {TEXT}")))
            out.append(ui.t(("      ", ""), (f"{c.tip_txs:,} tx  fees ", DIM), (fmt.sats(c.tip_fees_sat), f"bold {ORANGE}")))
        if da:
            chg = float(da.get("difficultyChange", 0)) / 100
            out.append(ui.t(("DIFF  ", f"bold {FAINT}"), (f"{int(da.get('remainingBlocks', 0)):,} blk", f"bold {TEXT}"), ("  est ", DIM),
                            ui.signed_pct(chg)))
        return out

    def menu(self) -> list[tuple[str, str]]:
        return [("FEES", "FEES"), ("BLK", "BLK"), ("RBF", "RBF"), ("MINE", "MINE")]

    def export(self):
        return (["block", "vsize", "txs", "median_fee", "fee_low", "fee_high", "total_fees_sat"],
                [[i + 1, b.get("blockVSize"), b.get("nTx"), b.get("medianFee"), (b.get("feeRange") or [None])[0],
                  (b.get("feeRange") or [None])[-1], b.get("totalFees")] for i, b in enumerate(self.hub.chain.mempool_blocks)])


class FeesPane(FuncPane):
    code, every = "FEES", 30
    TYPES = ("p2wpkh", "p2tr", "p2sh-p2wpkh", "p2pkh", "p2wsh-2of3")

    def __init__(self, hub, security=None, args=()) -> None:
        super().__init__(hub, security, args)
        nums = [int(a) for a in self.args if a.isdigit()]
        self.n_in, self.n_out = (nums + [1, 2])[0] or 1, (nums + [1, 2])[1] if len(nums) > 1 else 2
        kinds = [a.lower() for a in self.args if a.lower() in self.TYPES]
        self.kind = kinds[0] if kinds else "p2wpkh"
        self.recommended: dict = {}
        self.precise: dict = {}
        self.rates: list = []

    async def load(self) -> None:
        self.title = "what a transaction costs now"
        mp = self.hub.sources[self.hub.bitcoin_order()[0]]
        self.recommended, p1 = await mp.fees()
        self.provs = [p1]
        try:
            self.precise, _ = await mp.fees_precise()
        except (SourceError, AttributeError):
            self.precise = {}
        try:
            self.rates, _ = await mp.fee_rates("24h")
        except (SourceError, AttributeError):
            self.rates = []

    def draw(self, w: int, h: int) -> list[Text]:
        rec, pre = self.recommended, self.precise or self.recommended
        if not rec:
            return []
        price = self.hub.btc_price()
        out = [ui.section("RECOMMENDED", w, "sat/vB")]
        rows = []
        for key, label, eta in (("fastestFee", "next block", "~10m"), ("halfHourFee", "half hour", "~30m"), ("hourFee", "hour", "~1h"),
                                ("economyFee", "economy", "hours"), ("minimumFee", "minimum", "may wait days")):
            rate = float(pre.get(key, rec.get(key, 0)) or 0)
            out_kind = "p2tr" if self.kind == "p2tr" else "p2wpkh" if "w" in self.kind else "p2pkh"
            vsize = btcmath.tx_vsize(self.n_in, self.n_out, self.kind, out_kind)
            cost = btcmath.fee_sats(vsize, rate)
            rows.append([label, Text(fee_text(rate), style=f"bold {charts.snap(charts.fee_colour(rate))}"), str(rec.get(key, "–")), eta,
                         ui.sats_usd(cost, price)])
        cols = [ui.Col("target", "left"), ui.Col("precise"), ui.Col("rounded", style=DIM), ui.Col("expected", "left", style=DIM),
                ui.Col("cost of this tx", "left", flex=True)]
        if w < 70:                                          # a narrow pane keeps the numbers and drops the prose
            cols, rows = cols[:3] + cols[4:], [r[:3] + r[4:] for r in rows]
        out += ui.table(cols, rows, w)
        vsize = btcmath.tx_vsize(self.n_in, self.n_out, self.kind)
        out += [Text(""), ui.section("CALCULATOR", w, f"{vsize:,.1f} vB"),
                ui.fit([ui.t(("[ ] ", f"bold {ORANGE}"), (f"inputs {self.n_in}  ", TEXT)), ui.t(("{ } ", f"bold {ORANGE}"),
                        (f"outputs {self.n_out}  ", TEXT)), ui.t(("y ", f"bold {ORANGE}"), (self.kind, TEXT)),
                        ("      or: FEES 3 2 p2tr", FAINT)], w)]
        if self.rates:
            out += [Text(""), ui.section("RECENT BLOCKS BY PERCENTILE", w, "24 h · whole sat/vB")]
            for label, key in (("max", "avgFee_100"), ("p90", "avgFee_90"), ("median", "avgFee_50"), ("p10", "avgFee_10"), ("min", "avgFee_0")):
                vals = [float(r.get(key, 0)) for r in self.rates]
                out.append(ui.t((f"{label:<7}", FAINT), (charts.spark(vals, max(w - 24, 10)), charts.snap(charts.fee_colour(max(vals[-1], 1)))),
                                (f"  {fee_text(vals[-1])} now", DIM)))
            out += ui.wrap("Percentiles are whole numbers at the source, so fees under 1 sat/vB read as 0 here. The table above is exact.",
                           w, FAINT)
        return out

    def key(self, k: str, ch: str | None) -> bool:
        if ch in ("[", "]"):
            self.n_in = max(1, min(self.n_in + (1 if ch == "]" else -1), 500))
        elif ch in ("{", "}"):
            self.n_out = max(1, min(self.n_out + (1 if ch == "}" else -1), 500))
        elif ch == "y":
            self.kind = self.TYPES[(self.TYPES.index(self.kind) + 1) % len(self.TYPES)]
        else:
            return False
        return True

    def hint(self) -> str:
        return "[ ] inputs · { } outputs · y script type"

    def export(self):
        return ["target", "sat_per_vb"], [[k, v] for k, v in (self.precise or self.recommended).items()]


class BtcPane(FuncPane):
    code, every, tick = "BTC", 60, 1.0

    def __init__(self, hub, security=None, args=()) -> None:
        super().__init__(hub, security, args)
        self.oracle: float | None = None
        self.ath: tuple[float, float] | None = None         # (price, unix time) of the highest daily close
        self.closes: list[float] = []
        self.hash: dict = {}
        self.ln: dict = {}
        self.extra: list[Provenance] = []

    async def load(self) -> None:
        self.title = "Bitcoin"
        hub, extra = self.hub, []
        hub.watch.add("BTC")
        if bv := hub.sources.get("bitview"):
            try:
                self.oracle, p = await bv.oracle_price()
                extra.append(p)
                s = await bv.series("price_close", "day1", start=0)
                pts = [(v, t) for v, t in zip(s.values, s.times, strict=True) if v]
                if pts:
                    self.ath, self.closes = max(pts), [v for v, _ in pts][-90:]
            except SourceError:
                pass
        mp = hub.sources[hub.bitcoin_order()[0]]
        for name, call in (("hash", lambda: mp.hashrate("3d")), ("ln", mp.ln_latest)):
            try:
                v, p = await call()
                setattr(self, name, v)
                if name == "hash":
                    extra.append(p)
            except (SourceError, AttributeError):
                pass
        self.extra = extra

    def draw(self, w: int, h: int) -> list[Text]:
        hub, c, now = self.hub, self.hub.chain, time.time()
        q = hub.quotes.get("BTC")
        self.provs = [p for p in ([q.prov] if q else []) + ([c.prov] if c.prov else []) + self.extra]
        if not q and not c.height:
            return []
        out: list[Text] = []
        if q:
            head = ui.t((ui.px(q.price, 2), f"bold {ORANGE}"), ("  ", ""), ui.signed(q.change, 0), (" ", ""), ui.signed_pct(q.pct, 2),
                        ("   24h ", FAINT), (f"{ui.px(q.low, 0)} – {ui.px(q.high, 0)}", DIM))
            out += [head, ui.t(*[x for n, p in q.parts for x in ((f"{n} ", FAINT), (f"{ui.px(p, 0)}   ", DIM))],
                               ("median of the venues that answered", FAINT))]
        col: list[Text] = []
        if self.oracle:
            gap = (self.oracle / q.price - 1) if q else None
            col.append(ui.t(ui.kv("chain oracle  ", ui.px(self.oracle, 0)), ("  ", ""), ui.signed_pct(gap, 2) if gap is not None else "",
                            ("  read from the chain alone", FAINT)))
        if self.ath and q:
            col.append(ui.t(ui.kv("all-time high ", ui.px(self.ath[0], 0)), ("  ", ""), ui.signed_pct(q.price / self.ath[0] - 1),
                            (f"  daily close, {ui.day(self.ath[1])}", FAINT)))
        if c.height and q:
            issued = btcmath.issued_sats(c.height) / 1e8
            col.append(ui.t(ui.kv("market cap    ", ui.usd(issued * q.price)), ui.kv("   supply", f"{issued:,.0f} BTC"),
                            (f"  {issued / 21e6:.2%} of 21M, protocol maximum issued", FAINT)))
            hv = btcmath.halving(c.height, (c.difficulty.get("timeAvg") or 600_000) / 1000)
            col.append(ui.t(ui.kv("halving       ", f"{hv.blocks_left:,} blocks"), (f"  ~{hv.est_seconds / 86400:,.0f} days", DIM),
                            (f"  at #{hv.next_height:,} the subsidy falls to {hv.subsidy_next / 1e8:g} BTC", FAINT)))
        if c.height:
            col.append(ui.t(ui.kv("tip           ", f"#{c.height:,}", f"bold {ORANGE}"),
                            (f"  {ui.when(c.tip_time, now)} ago  {c.tip_pool}", DIM)))
        if self.hash:
            col.append(ui.t(ui.kv("hashrate      ", ui.hashrate(float(self.hash.get("currentHashrate", 0)))),
                            ui.kv("   difficulty", charts.num(float(self.hash.get("currentDifficulty", 0))))))
        if da := c.difficulty:
            col.append(ui.t(ui.kv("next retarget ", f"{int(da.get('remainingBlocks', 0)):,} blocks"), ("  est ", DIM),
                            ui.signed_pct(float(da.get("difficultyChange", 0)) / 100), ("  previous ", DIM),
                            ui.signed_pct(float(da.get("previousRetarget", 0)) / 100)))
        if c.fees:
            col.append(ui.t(ui.kv("fees          ", f"{fee_text(c.fastest_fee)} sat/vB next"),
                            (f"   30m {fee_text(float(c.fees.get('halfHourFee', 0)))} · 1h {fee_text(float(c.fees.get('hourFee', 0)))}", DIM),
                            ui.kv("   mempool", ui.vbytes(c.mempool_vsize)), (f"  {c.mempool_count:,} tx", DIM)))
        if latest := (self.ln or {}).get("latest"):
            cap = float(latest.get("total_capacity", 0))
            col.append(ui.t(ui.kv("lightning     ", f"{cap / 1e8:,.0f} BTC"), (f"  {int(latest.get('channel_count', 0)):,} channels · "
                            f"{int(latest.get('node_count', 0)):,} nodes · as of {str(latest.get('added', ''))[:10]}", FAINT)))
        if fc := hub.forecast():
            end, med, lo, hi = fc[0]
            col.append(ui.t(ui.kv("glimpse       ", ui.px(med, 0), f"bold {ui.CYAN}"), (f"  80% {ui.px(lo, 0)} – {ui.px(hi, 0)}", DIM),
                            (f"  the market's median for the {fmt.close_label(int(end), True)} UTC close", FAINT)))
        out += [Text("")] + col
        if self.closes and h - len(out) >= 3:
            out += [Text(""), ui.t(("90 days  ", FAINT), (charts.spark(self.closes, max(w - 12, 10)), ORANGE))]
        return out

    def menu(self) -> list[tuple[str, str]]:
        return [("GP", "GP BTC"), ("MEMP", "MEMP"), ("ONCH", "ONCH"), ("OMON", "OMON"), ("FEES", "FEES"), ("BLK", "BLK"), ("MINE", "MINE"),
                ("TRSY", "TRSY")]


_DOWN = ("When a backend is down the next one in `bitcoin.order` answers, and the frame names the one that did. With none, the last "
         "values stay on screen, dimmed, with their age.")
register(Function(
    "BTC", "Bitcoin", "Bitcoin", "the Bitcoin page: price, chain, fees, halving, the Glimpse forecast", BtcPane, takes=("CRYPTO",),
    optional=True,
    needs=("quote", "tip", "fees", "oracle", "series"),
    help="The composite price is the median of the last trades on Coinbase, Kraken and Bitstamp, refreshed every 12 s, with each "
         "venue beside it. The change is measured from each venue's 24 hour or UTC-day open. The chain oracle is Bitview's BTC/USD "
         "price, derived from round-dollar output amounts in recent blocks with no exchange involved. The all-time high is the "
         "highest daily close in Bitview's price_close series. Supply and market cap are computed from the tip height and the "
         "halving schedule: the protocol maximum issued, a little more than what can be spent. Tip, fees, mempool and the next "
         "retarget arrive over the mempool WebSocket. Hashrate is mempool's three-day estimate. Lightning is mempool's latest "
         "statistics with their as-of date. The Glimpse line is the market's own median and 80% band for the nearest hourly close. "
         + _DOWN))
register(Function(
    "MEMP", "Mempool", "Bitcoin", "the mempool: next blocks, the next block drawn, fee bands", MempPane, needs=("mempool_blocks", "fees", "tip"),
    help="Projected blocks as a train: how full each is, its fee range in sat/vB, how many transactions, and when it should be "
         "mined at the pace of this difficulty epoch. Below, the next block drawn as a picture: every transaction in template "
         "order, highest fee rate first, each half-block pixel a fixed slice of the block's million virtual bytes, coloured from "
         "blue (1 sat/vB) through green, yellow and orange to magenta (500). Space at the end is room nobody has paid for yet. "
         "When a block is mined the height flashes and the picture starts again. Data: the mempool WebSocket (mempool-blocks and "
         "track-mempool-block), held only while this pane is open. If the stream drops, the terminal polls every 20 s and draws the "
         "picture from the block's seven fee percentiles, and the frame says polling. " + _DOWN))
register(Function(
    "FEES", "Fees", "Bitcoin", "recommended and precise fees, recent blocks by percentile, a fee calculator", FeesPane,
    args="[inputs outputs script]", needs=("fees",),
    help="Recommended fees for the next block, half an hour, an hour, economy and the relay minimum: the precise value to three "
         "decimals and the rounded one most wallets show, with what your transaction would cost in sats and dollars. The calculator "
         "sizes a transaction of N inputs and M outputs for p2wpkh, p2tr, p2sh-p2wpkh, p2pkh or 2-of-3 p2wsh from the script templates. "
         "[ and ] change inputs, { and } outputs, y the script type. The last section shows the fee rates of the last 24 hours of blocks "
         "by percentile. Data: /api/v1/fees/recommended, /fees/precise and /mining/blocks/fee-rates/24h, every 30 s. " + _DOWN))
