"""Bots without the screen: `glimpse-tui bots` lists them, `glimpse-tui about <bot>` explains one, `glimpse-tui run <bot>`
runs one in this shell, `glimpse-tui run <bot> --manage` lets it manage your portfolio in the series, and
`glimpse-tui run take_profit` sells, across your whole portfolio, what the market overpays at a profit.

The same runner, ledger and safety rules as the terminal's bots screen, for tmux, launchd or a spare laptop.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

from . import auth, bots, fmt
from .api import PAIRS, ApiError, Glimpse

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
    print("\nRead one:  glimpse-tui about <id>     Run one:  glimpse-tui run <id> [--series BTC] [--budget 20000] [--live]"
          "\nLet one manage your portfolio:  glimpse-tui run <id> --manage [--series BTC] [--budget 20000] [--live]"
          "\nTake profit on your whole portfolio, never buying:  glimpse-tui run take_profit [--live]")
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


async def status(runners: list) -> None:
    """Every minute, one line per runner: what it holds and what it has done. Returns when every runner has stopped."""
    from .botsview import portfolio_line, sold

    while any(r.running for r in runners):
        await asyncio.sleep(60)
        for r in runners:
            print(f"[{r.series or 'portfolio'}] {time.strftime('%H:%M:%S', time.gmtime())}{portfolio_line(r).plain}  ·  "
                  + (f"bought {fmt.sats(r.paid_sats)} " if r.buys else "") + f"{sold(r)} {r.trades} trades", flush=True)


async def run(args: argparse.Namespace) -> int:
    from .zoo.data import Feed

    bot = next((b for b in bots.discover() if args.bot in (b.id, b.name)), None)
    if bot is None or bot.error:
        print(f"No bot called {args.bot}." if bot is None else f"{bot.id} failed to load: {bot.error}", file=sys.stderr)
        return 2
    key, _ = auth.load_key()
    if bot.sells_only:
        return await take_profit(args, bot, key)
    if args.manage and not key:
        print("Managing your portfolio means reading it: set GLIMPSE_API_KEY or log in from the terminal (L).", file=sys.stderr)
        return 2
    if args.live:
        if not key:
            print("Live trading needs an API key: set GLIMPSE_API_KEY or log in from the terminal (L).", file=sys.stderr)
            return 2
        cfg = bots.BotConfig.load().with_budget(args.budget)
        loss = "" if cfg.profit_only else " profit_only is off: it may sell at a loss."
        if args.manage:
            print(f"{bot.name} will sell and buy your real positions, unattended, including ones you opened yourself: at most "
                  f"{fmt.sats(cfg.bankroll_sats)} of fresh sats, {fmt.sats(cfg.max_per_cycle_sats)} per close visited, "
                  f"{fmt.sats(cfg.max_per_hour_sats)} per hour." + loss)
        else:
            print(f"{bot.name} will place real orders with real sats, unattended: at most {fmt.sats(cfg.bankroll_sats)} at risk, "
                  f"{fmt.sats(cfg.max_per_cycle_sats)} per close visited, {fmt.sats(cfg.max_per_hour_sats)} per hour." + loss)
        if os.environ.get(LIVE_ENV) != "I_UNDERSTAND" and input("Type LIVE to arm it: ").strip() != "LIVE":
            print("Not armed.")
            return 1
    api = Glimpse(key, os.environ.get("GLIMPSE_BASE_URL"))
    try:
        every = await api.batches()
        series = args.series or "BTC"
        chosen = every if series.lower() == "all" else [b for b in every if b.short.lower() == series.lower()]
        if not chosen:
            print(f"No series called {series}. Try BTC, 'BTC 1D', ETH, SOL, XAU, 'XAU 1D' or all.", file=sys.stderr)
            return 2
        ledger = bots.Ledger()                          # one ledger for every series, so the budget is one budget
        feeds: dict[str, Feed] = {}
        runners = []
        for batch in chosen:
            asset = batch.asset or "BTC"
            feed = feeds.setdefault(asset, Feed(asset))
            tag = f"[{batch.short}] " if len(chosen) > 1 else ""
            runners.append(bots.Runner(bot=bot, api=api, batch_id=batch.batch_id, feed=feed, live=args.live, ledger=ledger,
                                       cfg=bots.BotConfig.load().with_budget(args.budget / len(chosen)), series=batch.short,
                                       manage=args.manage, on_log=lambda line, tag=tag: print(tag + line, flush=True)))
        if args.once:
            for r in runners:
                r.say(f"one visit  {'LIVE' if r.live else 'dry run'}  budget {fmt.sats(r.cfg.bankroll_sats)}")
                await r.cycle()
        else:
            for r in runners:
                r.start()
            await status(runners)
    except ApiError as e:
        print(e, file=sys.stderr)
        return 1
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await api.close()
    return 0


async def take_profit(args: argparse.Namespace, bot: bots.Bot, key: str | None) -> int:
    """The Profit Taker in this shell: every series unless --series names some, no budget, sells only."""
    if not key:
        print("Taking profit means reading your portfolio: set GLIMPSE_API_KEY or log in from the terminal (L).", file=sys.stderr)
        return 2
    cfg = bots.BotConfig.load()
    if args.live:
        print(f"{bot.name} will sell your real positions, unattended, including ones you opened yourself: the part of each "
              "the market overpays" + (" at a profit, net of both fees. It never buys." if cfg.profit_only else
                                       ". profit_only is off: it may sell at a loss. It never buys."))
        if os.environ.get(LIVE_ENV) != "I_UNDERSTAND" and input("Type LIVE to arm it: ").strip() != "LIVE":
            print("Not armed.")
            return 1
    api = Glimpse(key, os.environ.get("GLIMPSE_BASE_URL"))
    try:
        every = [b for b in await api.batches() if b.asset in PAIRS]
        wanted = (args.series or "all").lower()
        chosen = every if wanted == "all" else [b for b in every if b.short.lower() in {x.strip() for x in wanted.split(",")}]
        if not chosen:
            print(f"No series called {args.series}. Try BTC, 'BTC 1D', ETH, SOL, XAU, 'XAU 1D', a list like 'BTC,XAU', or all.",
                  file=sys.stderr)
            return 2
        r = bots.ProfitTaker(bot=bot, api=api, batch_id="", feed=None, live=args.live, ledger=bots.Ledger(), cfg=cfg,
                             batches=chosen, on_log=lambda line: print(line, flush=True))
        if args.once:
            r.say(f"one visit  {'LIVE' if r.live else 'dry run'}  {r._intro()}")
            await r.cycle()
        else:
            r.start()
            await status([r])
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
    rn.add_argument("--series", default=None, help="BTC, 'BTC 1D', ETH, SOL, XAU, 'XAU 1D', or all (the budget is split evenly); "
                                                  "BTC by default, every series for take_profit")
    rn.add_argument("--budget", type=float, default=bots.BotConfig.load().bankroll_sats, help="sats the bot may have at risk")
    rn.add_argument("--live", action="store_true", help=f"trade real sats (asks you to type LIVE, or set {LIVE_ENV}=I_UNDERSTAND)")
    rn.add_argument("--once", action="store_true", help="one visit (the nearest close), then exit")
    rn.add_argument("--manage", action="store_true",
                    help="manage your whole portfolio in the series: sell what the market overpays at a profit, buy what it "
                         "underprices; --budget is then the fresh sats it may add (needs an API key, even on paper)")
    args = p.parse_args(argv)
    if args.cmd == "bots":
        return list_bots(args.search)
    if args.cmd == "about":
        return about_bot(args.bot)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 0
