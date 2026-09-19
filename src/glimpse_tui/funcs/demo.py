"""DEMO: two minutes through the terminal for someone who has never seen it (TERMINAL.md 13, Phase 7).

`glimpse-tui DEMO` from a shell, or DEMO <GO> inside. Each step runs a real GO bar command against live data and
says on the status line what is on screen. Any key stops it and leaves you where it was.
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from ..data.core import SourceError
from ..term.registry import Function, register

if TYPE_CHECKING:
    from ..term.gobar import Command
    from ..term.shell import Shell

# (command, zoom the pane, seconds, what to say). `TX` is filled in with a transaction from the newest block.
STEPS: tuple[tuple[str, bool, float, str], ...] = (
    ("LP BTC", False, 16, "The default launchpad. Price into Glimpse's own forecast, the mempool, on-chain, and the news. ` opens the GO bar."),
    ("BTC", True, 14, "BTC <GO>: the Bitcoin page. A composite price, the price read from the chain alone, supply, the halving, fees."),
    ("MEMP", True, 16, "MEMP <GO>: the next blocks as a train, and the next block drawn, every transaction, coloured by its fee."),
    ("TX", True, 16, "TX <txid> <GO> explains any transaction: inputs, outputs, fee rate, RBF, CPFP. This one is from the newest block."),
    ("ONCH", True, 16, "ONCH <GO>: the cycle metrics from Bitview's open series, each named and dated. FLDS searches 60,000 more."),
    ("LP MACRO", False, 18, "LP MACRO: the curve, liquidity, FX, commodities and indices. Every number says its source and how old it is."),
    ("TRSY", True, 14, "TRSY <GO>: companies that hold Bitcoin, from their SEC filings, once you give the SEC a contact (SET)."),
    ("HM", False, 16, "HM <GO>: Glimpse's forecast heatmap. v draws a box, tab opens the bet slip. t returns to the launchpad. q quits."),
)


async def newest_txid(shell: Shell) -> str:
    hub = shell.hub
    for key in hub.bitcoin_order():
        try:
            blocks, _ = await hub.sources[key].blocks()
            txs, _ = await hub.sources[key].block_txs(blocks[0]["id"], 0)
            return str(txs[1]["txid"] if len(txs) > 1 else txs[0]["txid"])
        except (SourceError, AttributeError, LookupError, KeyError):
            continue
    return ""


async def run(shell: Shell, steps: tuple[tuple[str, bool, float, str], ...] = STEPS, pace: float = 1.0) -> None:
    app = shell.app
    shell.demo = True
    try:
        for cmd, zoom, secs, caption in steps:
            if not shell.demo:
                break
            if cmd == "TX":
                txid = await newest_txid(shell)
                if not txid:
                    continue
                cmd = f"TX {txid}"
            shell.run(cmd)
            if app.view == "term" and shell.ws.root and shell.ws.zoomed != zoom:
                shell.ws.zoomed = zoom
                shell.ws.relayout()
            app.say(f"DEMO  {caption}   (any key stops)", secs * pace + 1)
            waited = 0.0
            while waited < secs * pace and shell.demo:
                await asyncio.sleep(0.2)
                waited += 0.2
    finally:
        was, shell.demo = shell.demo, False
        if was:
            app.say("That was the tour. HELP lists every function. ` opens the GO bar.", 12)


def start(shell: Shell, cmd: Command) -> bool:
    shell.ensure_started()
    shell.app.run_worker(run(shell), group="demo", exclusive=True)
    return True


def _install() -> None:
    from ..term import shell
    shell.SHELL_RUN["DEMO"] = start


_install()
register(Function("DEMO", "Demo", "Tools", "a two-minute tour: BTC, MEMP, TX, ONCH, MACRO, TRSY and HM", None,
                  help="Walks through the default launchpad, the Bitcoin page, the mempool with the next block drawn, a transaction from "
                       "the newest block, the on-chain dashboard, the MACRO launchpad, the treasury companies and the forecast heatmap, "
                       "about fifteen seconds each, with a line on the status bar saying what is on screen. Everything shown is live. "
                       "Any key stops the tour and leaves you where it was. From a shell: glimpse-tui DEMO."))
