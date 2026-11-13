"""`DFF`, the coming retargets: the chain's pace, what Bitcoin's rule makes of it, and both markets beside it, with the price of cover.

The closes come the way FCST gets them, once for each series. The simulation runs on this computer from Bitview's blocks, in a
worker thread, and a run reads every retarget on both ladders. Nothing here trades: `glimpse-tui run difficulty` and `run adjustment` do.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

from rich.text import Text

from .. import difficulty as H
from .. import pricing as P
from ..api import ADJUSTMENT, DIFFICULTY, ApiError
from ..data.core import SourceError
from ..term import ui
from ..term.registry import Function, register
from ..theme import DIM, FAINT, GREEN, ORANGE, RED, TEXT
from .forecast import FcstPane
from .hashprice import spaced

COVER_FROM = 0.05  # the cover the page prices: every range from this far over today's level, on the next retarget
COVER_WIDTH = 30  # ...across this many 1T ranges


def cover_quote(view, probs, strike: float) -> tuple[float, float] | None:
    """(what one contract on every range from `strike` up costs, per sat it pays if the retarget lands there; what it is worth by
    the picture, likewise). 1.00 and 1.00 is fair. The extra over the second is what a miner pays for the cover to exist."""
    row = view.row
    idx = [i for i, (a, _) in enumerate(view.bins) if strike <= a < strike + COVER_WIDTH]
    if not idx:
        return None
    t = P.ticket(list(row.shares), P.alpha_for(len(row.shares), row.topic_id), idx[0], idx[-1], 1.0, prob=0.0)
    chance = float(sum(probs[i] for i in idx))
    return t.cost_sats / (P.PAYOUT_SATS * (1 - P.FEE)), chance


class DffPane(FcstPane):
    code, every, tick = "DFF", 60, 15.0
    wants_candles = False

    def __init__(self, hub, security=None, args=()) -> None:
        super().__init__(hub, None, (DIFFICULTY,))
        self.adjustments: list = []
        self.inputs: H.Inputs | None = None
        self.state: H.State | None = None
        self.result: H.Run | None = None
        self.title = "Difficulty · the coming retargets"

    async def load(self) -> None:
        from ..app import summarise

        await super().load()
        self.inputs = self.inputs or H.shared_inputs()
        await self.inputs.refresh()
        if not self.inputs.ready:
            raise SourceError("Bitview has not answered: the forecast needs its blocks")
        api = self.hub.glimpse()
        got = await self.hub_batches(api)
        batch = next((b for b in got if b.asset == ADJUSTMENT), None)
        if batch is not None:
            try:
                rows = await api.markets(batch.batch_id, limit=H.MARKETS)
                self.adjustments = await asyncio.to_thread(lambda: [summarise(r) for r in rows if r.shares])
            except ApiError:
                pass
        now = time.time()
        self.result = await asyncio.to_thread(self.inputs.run, now)
        self.state = self.inputs.state(now)
        self.title = "Difficulty · the coming retargets"

    async def hub_batches(self, api) -> list:
        await self._batch(api)
        return self.hub.fcst_batches[1]

    def cache_key(self) -> tuple:
        st = self.state
        return (id(self.views), len(self.views), len(self.adjustments), st.height if st else 0, int(time.time() // 60))

    def menu(self) -> list[tuple[str, str]]:
        return [("HEATMAP", f"FCST {DIFFICULTY}"), ("ADJUSTMENT", f"FCST {ADJUSTMENT}"), ("ODDS", f"DIST {DIFFICULTY}"), ("HISTORY", "DIFF")]

    def hint(self) -> str:
        return "glimpse-tui run difficulty, run adjustment trade this on paper  ·  HELP DFF explains it"

    def _rows(self, w: int, h: int) -> list[Text]:
        import numpy as np

        run = self.result
        wide = w >= 80
        shown = max((h - 10) // 2, 3)  # every retarget if the window has room, the nearest of both series if not
        head = ui.t(
            (f"{'retarget':<10}{'about':<8}", FAINT),
            (f"{'model':>9}", FAINT),
            (f"{'80% band':>16}" if wide else "", FAINT),
            (f"{'market':>9}", FAINT),
            (f"{'80% band':>16}" if wide else "", FAINT),
            (f"{'gap':>8}", FAINT),
        )
        out = [ui.section("THE DIFFICULTY", w, "T, the model against the market"), head]

        def line(v, x, unit: str) -> Text | None:
            lo, mid, hi = np.quantile(x, (0.1, 0.5, 0.9))
            when = datetime.fromtimestamp(float(np.median(run.arrival_at(v.row.close_block))), UTC).strftime("%d %b")
            gap = mid - v.median
            tell = GREEN if gap > 0.25 else RED if gap < -0.25 else FAINT
            return ui.t(
                (f"#{v.row.close_block:<9,}{when:<8}", DIM),
                (f"{mid:>9.2f}", f"bold {ORANGE}"),
                (f"{f'{lo:.1f} to {hi:.1f}':>16}" if wide else "", DIM),
                (f"{v.median:>9.2f}", TEXT),
                (f"{f'{v.band[0]:.0f} to {v.band[1]:.0f}':>16}" if wide else "", FAINT),
                (f"{gap:>+8.2f}{unit}", tell),
            )

        for v in self.views[:shown]:
            if v.row.close_block in run.heights:
                out.append(line(v, run.difficulty_at(v.row.close_block) / 1e12, "T"))
        if self.adjustments:
            out += [ui.section("THE ADJUSTMENT", w, "%, the model against the market"), head]
            for v in self.adjustments[:shown]:
                if v.row.close_block in run.heights:
                    out.append(line(v, (run.adjustment_at(v.row.close_block) - 1) * 100, "%"))
        return out

    def _cover(self, w: int) -> list[Text]:
        """What a miner pays for cover against the next retarget: the bots' second job is to bring this down."""
        import numpy as np

        run, st = self.result, self.state
        near = [v for v in self.views if v.row.close_block == st.next_retarget]
        if not near:
            return []
        v = near[0]
        strike = st.difficulty / 1e12 * (1 + COVER_FROM)
        edges = np.array([v.bins[0][0]] + [b for _, b in v.bins])
        got = cover_quote(v, H.probabilities(run.difficulty_at(st.next_retarget) / 1e12, edges), strike)
        if got is None:
            return []
        price, chance = got
        return [
            ui.section("MINER COVER", w, f"next retarget, over {strike:,.0f}T: +{COVER_FROM:.0%} or more"),
            ui.fit(
                spaced(
                    ui.kv("costs", f"{price:.2f} sats a sat of payout", f"bold {TEXT}"),
                    ui.kv("the model's chance", f"{chance:.1%}", f"bold {ORANGE}"),
                    ui.kv("so it pays back", f"{chance / price:.2f} a sat" if price else "–"),
                ),
                w,
            ),
        ]

    def draw(self, w: int, h: int) -> list[Text]:
        st = self.state
        if st is None or self.result is None:
            return []
        at, change = st.retarget_estimate()
        out = [ui.section("THE CHAIN", w, "Bitview, block by block")]
        out.append(
            ui.fit(
                spaced(
                    ui.kv("difficulty", f"{st.difficulty / 1e12:,.2f}T", f"bold {ORANGE}"),
                    ui.kv("tip", f"#{st.height:,}"),
                    ui.kv("hash rate", f"{st.pace:.1%} of schedule", f"bold {TEXT}"),
                ),
                w,
            )
        )
        out.append(
            ui.fit(
                spaced(
                    ui.kv("next retarget", f"#{st.next_retarget:,}"),
                    ui.kv("in", f"{st.blocks_left:,} blocks"),
                    ui.kv("about", datetime.fromtimestamp(at, UTC).strftime("%d %b %H:%M UTC")),
                    ui.t(("at this pace ", FAINT), ui.signed_pct(change)),
                ),
                w,
            )
        )
        return out + self._cover(w) + self._rows(w, h)


register(
    Function(
        "DFF",
        "Difficulty forecast",
        "Glimpse",
        "the coming retargets from Bitcoin's own rule, against the difficulty and adjustment markets",
        DffPane,
        needs=("glimpse",),
        help="Glimpse runs two markets on every retarget: the difficulty it sets, in terahash, and the percent change it makes. Both settle on "
        "the chain's own number, and trading stops three blocks before it. The rule is exact: the old difficulty times two weeks over "
        "the time the epoch took. So the page simulates the blocks still to come at the hash rate the recent blocks show, in height "
        "rather than hours, and applies the rule on each path. The top shows today's difficulty, the hash rate against schedule, and "
        "the next retarget at the current pace. The tables give every coming retarget: the model's median and 80% band, the market's "
        "own, and the gap. Further out the adjustment's band stays about as wide while the difficulty's keeps widening: a far adjustment "
        "depends on one epoch's luck, and a lucky epoch is largely taken back by the next. Miner cover prices the hedge a miner would "
        "buy against a rising difficulty on the next retarget, every range over +5%: what it costs per sat it pays, and how likely "
        "the model says it is to pay. Blocks come from Bitview every 30 s; if Bitview is down the page says so and the forecast stays "
        "dark. `glimpse-tui difficulty backtest` scores this forecast on past retargets, and `difficulty hedge` shows what leaning "
        "against miners does.",
    )
)
