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
    ("DIST BTC", True, 14, "The terminal opens here: every outcome of Bitcoin's next hour, with what the market gives it."),
    ("LP BTC", False, 16, "t is the rest of it. One long page, most important first: Bitcoin, gold, the chain, the news, the world."),
    ("GP BTC 24H", True, 14, "The last 24 hours and the next 24: the price to NOW, then Glimpse's median and 80% band."),
    ("PL BTC", True, 16, "The power law: Bitcoin against its own age, log-log, with the least-squares line through it."),
    ("PL XAUBTC", True, 14, "Gold priced in Bitcoin, in satoshis. The same law from the other side: the exponent is negative."),
    ("QM GLOBAL SATS", False, 12, "$ prices any market table in satoshis. XAUBTC, SPXBTC, NVDABTC: everything, in bitcoin."),
    ("CONS BTC", True, 14, "LP BOTS: every bot on this machine, read against the market, and where they agree."),
    ("NEWS", False, 12, ": opens anything. NEWS: the headlines, and enter reads a story right here. backspace goes back."),
    ("LP MACRO", False, 14, "SPC is the leader: SPC w for windows, SPC b for buffers, SPC t f o B p for the screens."),
    ("HM", False, 14, "f is the forecast: the whole heatmap. v draws a box, tab opens the bet slip. SPC t returns. q quits."),
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
register(Function("DEMO", "Demo", "Tools", "a ninety-second tour: the page, the odds, the chart, news, HM",
                  None,
                  help="Walks through the default launchpad, the next hour's odds, gold maximized, the news, the "
                       "MACRO launchpad and the full forecast heatmap, about fifteen seconds each, with a line on the status bar saying what "
                       "is on screen. Everything shown is live. "
                       "Any key stops the tour and leaves you where it was. From a shell: glimpse-tui DEMO."))
