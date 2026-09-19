"""Bots without the screen: `glimpse-tui bots` lists them, `glimpse-tui about <bot>` explains one, `glimpse-tui run <bot>`
runs one in this shell.

The same runner, ledger and safety rules as the terminal's bots screen, for tmux, launchd or a spare laptop.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

from . import auth, bots, fmt
from .api import ApiError, Glimpse

LIVE_ENV = "GLIMPSE_BOT_LIVE"


def list_bots(needle: str = "") -> int:
    from .botsview import matches

    family = None
    for b in bots.discover():
        if needle and not matches(b, needle):
            continue
        if b.family != family:
            family = b.family
            print(f"\n{family.upper()}")
        print(f"  {b.id:<28} {b.name:<30} {b.blurb}")
    print("\nRead one:  glimpse-tui about <id>     Run one:  glimpse-tui run <id> [--series BTC] [--budget 20000] [--live]")
    return 0


def about_bot(name: str) -> int:
    """The model's account of itself, as the terminal's i key shows it, without the stance (no market is loaded)."""
    import shutil

    from rich.console import Console

    from .botsview import about

    bot = next((b for b in bots.discover() if name in (b.id, b.name)), None)
    if bot is None:
        print(f"No bot called {name}.", file=sys.stderr)
        return 2
    width = min(shutil.get_terminal_size((120, 40)).columns, 120)
    Console(width=width).print(about(bot, None, 0.0, width))
    return 0


async def run(args: argparse.Namespace) -> int:
    from .zoo.data import Feed

    bot = next((b for b in bots.discover() if args.bot in (b.id, b.name)), None)
    if bot is None or bot.error:
        print(f"No bot called {args.bot}." if bot is None else f"{bot.id} failed to load: {bot.error}", file=sys.stderr)
        return 2
    key, _ = auth.load_key()
    if args.live:
        if not key:
            print("Live trading needs an API key: set GLIMPSE_API_KEY or log in from the terminal (L).", file=sys.stderr)
            return 2
        cfg = bots.BotConfig.load().with_budget(args.budget)
        print(f"{bot.name} will place real orders with real sats, unattended: at most {fmt.sats(cfg.bankroll_sats)} at risk, "
              f"{fmt.sats(cfg.max_per_cycle_sats)} per cycle, {fmt.sats(cfg.max_per_hour_sats)} per hour.")
        if os.environ.get(LIVE_ENV) != "I_UNDERSTAND" and input("Type LIVE to arm it: ").strip() != "LIVE":
            print("Not armed.")
            return 1
    api = Glimpse(key, os.environ.get("GLIMPSE_BASE_URL"))
    try:
        batch = next((b for b in await api.batches() if b.short.lower() == args.series.lower()), None)
        if batch is None:
            print(f"No series called {args.series}. Try BTC, 'BTC 1D', ETH, SOL or XAU.", file=sys.stderr)
            return 2
        r = bots.Runner(bot=bot, api=api, batch_id=batch.batch_id, feed=Feed(batch.asset or "BTC"), live=args.live,
                        cfg=bots.BotConfig.load().with_budget(args.budget), series=batch.short, on_log=print)
        if args.once:
            r.say(f"one cycle  {'LIVE' if r.live else 'dry run'}  budget {fmt.sats(r.cfg.bankroll_sats)}")
            await r.cycle()
        else:
            r.start()
            await r.wait()
    except ApiError as e:
        print(e, file=sys.stderr)
        return 1
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await api.close()
    return 0


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="glimpse-tui", description="Glimpse bots from the command line.")
    sub = p.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("bots", help="list every bot")
    ls.add_argument("search", nargs="?", default="", help="filter by name, family or idea")
    ab = sub.add_parser("about", help="how one bot works: the idea, what it reads, the maths, what it trades")
    ab.add_argument("bot", help="a bot id from `glimpse-tui bots`")
    rn = sub.add_parser("run", help="run one bot in this shell (ctrl-c stops it)")
    rn.add_argument("bot", help="a bot id from `glimpse-tui bots`")
    rn.add_argument("--series", default="BTC")
    rn.add_argument("--budget", type=float, default=bots.BotConfig.load().bankroll_sats, help="sats the bot may have at risk")
    rn.add_argument("--live", action="store_true", help=f"trade real sats (asks you to type LIVE, or set {LIVE_ENV}=I_UNDERSTAND)")
    rn.add_argument("--once", action="store_true", help="one cycle, then exit")
    args = p.parse_args(argv)
    if args.cmd == "bots":
        return list_bots(args.search)
    if args.cmd == "about":
        return about_bot(args.bot)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 0
