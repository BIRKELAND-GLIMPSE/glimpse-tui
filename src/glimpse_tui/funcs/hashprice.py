"""`HPF`, the hashprice forecast: the oracle's number, its parts, and every close as the simulation sees it beside the market.

The market's closes come the way FCST gets them. The simulation runs on this computer from Bitview's blocks and Coinbase's
quote, in a worker thread, and a run reads every close. Nothing here trades: `glimpse-tui run hashprice` does.
"""
from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

from rich.text import Text

from .. import fmt
from .. import hashprice as H
from ..api import HASHPRICE
from ..data.core import SourceError
from ..term import ui
from ..term.registry import Function, register
from ..theme import DIM, FAINT, GREEN, ORANGE, RED, TEXT
from .forecast import FcstPane


def spaced(*parts: Text) -> list:
    """Parts for `ui.fit`, a gap between each."""
    return [x for p in parts for x in (p, "   ")][:-1]


class HpfPane(FcstPane):
    code, every, tick = "HPF", 60, 15.0
    wants_candles = False

    def __init__(self, hub, security=None, args=()) -> None:
        super().__init__(hub, None, (HASHPRICE,))
        self.inputs: H.Inputs | None = None
        self.state: H.State | None = None
        self.bands: dict[float, tuple[float, float, float]] = {}
        self.title = "Hashprice · forecast from its parts"

    async def load(self) -> None:
        await super().load()
        self.inputs = self.inputs or H.Inputs()
        await self.inputs.refresh()
        if not self.inputs.ready:
            raise SourceError("Bitview has not answered: the forecast needs its blocks")
        self.state, self.bands = await asyncio.to_thread(self._forecast, [v.row.end_time_utc for v in self.views])
        self.title = "Hashprice · forecast from its parts"

    def _forecast(self, closes: list[int]) -> tuple[H.State, dict]:
        import numpy as np

        now = time.time()
        bands = {}
        for c in closes:
            lo, mid, hi = np.quantile(self.inputs.samples(now, float(c)), (0.1, 0.5, 0.9))
            bands[float(c)] = (float(lo), float(mid), float(hi))
        return self.inputs.state(now), bands

    def cache_key(self) -> tuple:
        st = self.state
        return (id(self.views), len(self.views), st.height if st else 0, round(st.price if st else 0.0), int(time.time() // 60))

    def menu(self) -> list[tuple[str, str]]:
        return [("HEATMAP", f"FCST {HASHPRICE}"), ("ODDS", f"DIST {HASHPRICE}"), ("HISTORY", "HASHP")]

    def hint(self) -> str:
        return "glimpse-tui run hashprice trades this on paper  ·  HELP HPF explains it"

    def draw(self, w: int, h: int) -> list[Text]:
        st = self.state
        if st is None:
            return []
        out = [ui.section("THE ORACLE", w, "Bitview hash_price, $ per PH/s a day")]
        steady = H.K * (float(H.subsidy_btc(st.height)) + st.fee) * st.price / st.difficulty
        out.append(ui.fit(spaced(ui.kv("now", f"${st.hashprice:,.2f}", f"bold {ORANGE}"),
                                 ui.kv("at today's price and difficulty", f"${steady:,.2f}", f"bold {TEXT}")), w))
        out.append(ui.section("ITS PARTS", w))
        out.append(ui.fit(spaced(ui.kv("Bitcoin", f"${st.price:,.0f}"), ui.kv("subsidy", f"{float(H.subsidy_btc(st.height)):g} BTC"),
                                 ui.kv("fees", f"{st.fee:.3f} BTC a block"), ui.kv("difficulty", f"{st.difficulty / 1e12:,.1f}T")), w))
        at, change = st.retarget_estimate(self.inputs.params.rate_tau_h)
        rate = st.rate(self.inputs.params.rate_tau_h) / st.difficulty
        out.append(ui.fit(spaced(ui.kv("next retarget", f"#{st.next_retarget:,}"), ui.kv("in", f"{st.blocks_left:,} blocks"),
                                 ui.kv("about", datetime.fromtimestamp(at, UTC).strftime("%d %b %H:%M UTC")),
                                 ui.t(("estimate ", FAINT), ui.signed_pct(change)), ui.kv("pace", f"{rate:.1%} of schedule")), w))
        out.append(ui.section("EVERY CLOSE", w, "the model against the market"))
        wide = w >= 62
        out.append(ui.t((f"{'close':<9}{'model':>8}", FAINT), (f"{'80% band':>16}" if wide else "", FAINT), (f"{'market':>9}", FAINT),
                        (f"{'80% band':>16}" if wide else "", FAINT), (f"{'gap':>8}", FAINT)))
        now = time.time()
        for v in self.live(now):
            band = self.bands.get(float(v.row.end_time_utc))
            if band is None:
                continue
            lo, mid, hi = band
            gap = mid / v.median - 1 if v.median else 0.0
            label = datetime.fromtimestamp(v.row.end_time_utc, UTC).strftime("%d %b")
            out.append(ui.t((f"{label:<9}", DIM), (f"{mid:>8.2f}", f"bold {ORANGE}"),
                            (f"{fmt.span(lo, hi):>16}" if wide else "", DIM), (f"{v.median:>9.2f}", TEXT),
                            (f"{fmt.span(*v.band):>16}" if wide else "", FAINT),
                            (f"{gap:>+8.1%}", GREEN if gap > 0.005 else RED if gap < -0.005 else FAINT)))
        return out


register(Function(
    "HPF", "Hashprice forecast", "Glimpse", "the hashprice forecast from its parts: price, retarget, subsidy, fees, against the market", HpfPane,
    needs=("glimpse",),
    help="Glimpse settles each hashprice market on Bitview's hash_price at the hourly slot ending at the close. That number is the "
         "dollars of coinbase over the last 24 hours of blocks divided by their difficulty, times a constant, so the page forecasts "
         "the parts: Bitcoin's price from its volatility clock, the hash rate from the recent blocks, the retarget from Bitcoin's "
         "own rule, the subsidy from its schedule, fees from their recent level. Blocks already mined inside a future window are "
         "used as they are. The top shows the oracle now, what it settles to if the price and difficulty stay where they are, and "
         "the next retarget with its estimated change. The table gives every open close: the simulation's median and 80% band, "
         "the market's own median and band, and how far the model's median sits from the market's. Blocks come from Bitview every "
         "30 s and the quote from Coinbase; one simulation of 6,000 paths reads every close and takes under a second. If Bitview "
         "is down the page says so and the forecast stays dark. `glimpse-tui hashprice backtest` scores this forecast on past closes."))
