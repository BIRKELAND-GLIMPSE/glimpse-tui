"""Bots without the screen: `glimpse-tui bots` lists them, `glimpse-tui about <bot>` explains one, `glimpse-tui run <bot>`
runs one in this shell, and `glimpse-tui run <bot> --manage` lets it manage your portfolio in the series. `glimpse-tui run
hashprice` trades the hashprice markets, and `glimpse-tui hashprice` is the bench its forecast is built on: `now`, `backtest`,
`fit`. `glimpse-tui run difficulty` and `run adjustment` trade the retarget markets, and `glimpse-tui difficulty` is their bench.

`glimpse-tui desk <bot>` runs one bot across every series at once as a desk (`glimpse_tui.desk.Desk`): every open close's
book kept current over the market WebSocket, every close re-planned on every trade in it and the moment it is launched,
sized by fractional Kelly on the wallet's balance.

The same runner, ledger and safety rules as the terminal's bots screen, for tmux, launchd or a spare laptop.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

from . import auth, bots, fmt
from .api import DMARKET_TOKEN_ENV, ApiError, Glimpse

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
          "\nTrade the hashprice markets:  glimpse-tui run hashprice [--budget 100000] [--live]   (the bench: glimpse-tui hashprice)"
          "\nTrade the retarget markets:  glimpse-tui run difficulty|adjustment [--budget 100000] [--live]"
          "   (the bench: glimpse-tui difficulty)")
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
                  f"bought {fmt.sats(r.paid_sats)} {sold(r)} {r.trades} trades", flush=True)


async def run(args: argparse.Namespace) -> int:
    from .zoo.data import Feed

    bot = next((b for b in bots.discover() if args.bot in (b.id, b.name)), None)
    if bot is None or bot.error:
        print(f"No bot called {args.bot}." if bot is None else f"{bot.id} failed to load: {bot.error}", file=sys.stderr)
        return 2
    key, _ = auth.load_key()
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
        series = args.series or bot.only_series or "BTC"
        if bot.only_series and series.lower() != bot.only_series.lower():
            print(f"{bot.name} can only draw {bot.only_series}.", file=sys.stderr)
            return 2
        chosen = every if series.lower() == "all" else [b for b in every if b.short.lower() == series.lower()]
        if chosen and series.lower() != "all" and not any(bot.draws(b.asset or "BTC") for b in chosen):
            print(f"{bot.name} has no picture of {series}: a retarget series is a step a walk knows nothing of, and only its own bot draws it.",
                  file=sys.stderr)
            return 2
        chosen = [b for b in chosen if bot.draws(b.asset or "BTC")]
        if not chosen:
            print(f"No series called {series}. Try BTC, 'BTC 1D', ETH, SOL, XAU, 'XAU 1D', XAUTBTC, WTIBTC, HASHPRICE, DIFFICULTY, "
                  "ADJUSTMENT or all.",
                  file=sys.stderr)
            return 2
        if args.live and any(b.kind for b in chosen) and not await api.can_trade_second_service():
            print(f"The hashprice and difficulty service did not accept these credentials, so no order would fill. "
                  f"It reads a session token, not an "
                  f"API key: set {DMARKET_TOKEN_ENV}, or leave out --live to trade on paper.", file=sys.stderr)
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


async def desk(args: argparse.Namespace) -> int:
    """The Desk in this shell: one bot, every series in --series (every price series by default), on every update."""
    from . import desk as D
    from .zoo.data import Feed

    bot = next((b for b in bots.discover() if args.bot in (b.id, b.name)), None)
    if bot is None or bot.error:
        print(f"No bot called {args.bot}." if bot is None else f"{bot.id} failed to load: {bot.error}", file=sys.stderr)
        return 2
    key, _ = auth.load_key()
    cfg = D.DeskConfig.load(series=args.series, kelly=args.kelly, min_edge=args.min_edge, reserve=args.reserve, max_hours=args.max_hours)
    if args.manage and not key:
        print("Managing your portfolio means reading it: set GLIMPSE_API_KEY or log in from the terminal (L).", file=sys.stderr)
        return 2
    if args.live:
        if not key:
            print("Live trading needs an API key: set GLIMPSE_API_KEY or log in from the terminal (L).", file=sys.stderr)
            return 2
        cap = f"the wallet's balance, capped at {fmt.sats(args.budget)}" if args.budget else "the wallet's whole balance"
        print(f"{bot.name} will trade real sats across {', '.join(cfg.series_list())}, unattended, on every market update: the "
              f"bankroll is {cap}, {cfg.reserve:.0%} of it kept back, {cfg.kelly:g} of the Kelly optimum on every close, no contract "
              f"bought past {cfg.min_edge:.0%} under value after fees, at most {cfg.hour_turnover:g}× the bankroll bought in an hour."
              + ("" if cfg.profit_only else " profit_only is off: it may sell at a loss."))
        if os.environ.get(LIVE_ENV) != "I_UNDERSTAND" and input("Type LIVE to arm it: ").strip() != "LIVE":
            print("Not armed.")
            return 1
    api = Glimpse(key, os.environ.get("GLIMPSE_BASE_URL"))
    try:
        every = await api.batches()
        chosen = D.chosen_batches(every, cfg, bot)
        if not chosen:
            print(f"No series among {cfg.series} that {bot.name} can draw. Try all, or a list like 'BTC,XAU,WTIBTC' from: "
                  f"{', '.join(D.PRICE_SERIES)}.", file=sys.stderr)
            return 2
        feeds: dict[str, Feed] = {}
        budget = args.budget if args.budget is not None else (0.0 if args.live else bots.BotConfig.load().bankroll_sats)
        d = D.Desk(bot=bot, api=api, batches=chosen, live=args.live, budget=budget, cfg=cfg, ledger=bots.Ledger(), manage=args.manage,
                   make_feed=lambda asset: feeds.setdefault(asset, Feed(asset)), on_log=lambda line: print(line, flush=True))
        if args.once:
            await d.once()
            print(d.status(), flush=True)
        else:
            d.start()
            while d.running:
                await asyncio.sleep(60)
                print(f"{time.strftime('%H:%M:%S', time.gmtime())}  {d.status()}", flush=True)
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
    rn.add_argument("--series", default=None,
                    help="BTC, 'BTC 1D', ETH, SOL, XAU, 'XAU 1D', XAUTBTC, WTIBTC, HASHPRICE, DIFFICULTY, ADJUSTMENT, or all "
                         "(the budget is split evenly); BTC by default; the hashprice, difficulty and adjustment bots read their own "
                         "series")
    rn.add_argument("--budget", type=float, default=bots.BotConfig.load().bankroll_sats, help="sats the bot may have at risk")
    rn.add_argument("--live", action="store_true", help=f"trade real sats (asks you to type LIVE, or set {LIVE_ENV}=I_UNDERSTAND)")
    rn.add_argument("--once", action="store_true", help="one visit (the nearest close), then exit")
    rn.add_argument("--manage", action="store_true",
                    help="manage your whole portfolio in the series: sell what the market overpays at a profit, buy what it "
                         "underprices; --budget is then the fresh sats it may add (needs an API key, even on paper)")
    dk = sub.add_parser("desk", help="run one bot across every series at once, on every market update, by fractional Kelly on the wallet")
    dk.add_argument("bot", help="a bot id from `glimpse-tui bots` (one that draws a picture of a close)")
    dk.add_argument("--series", default=None,
                    help="the series to trade: all (every price series: BTC, 'BTC 1D', ETH, SOL, XAU, 'XAU 1D', XAUTBTC, WTIBTC) or a "
                         "comma list like 'BTC,XAU,WTIBTC'; bots.toml [desk] series, else all")
    dk.add_argument("--budget", type=float, default=None,
                    help="paper: the bankroll in sats (bots.toml bankroll_sats, 100,000 by default); live: a cap on the wallet's balance "
                         "as the bankroll (the whole balance by default)")
    dk.add_argument("--kelly", type=float, default=None, help="the fraction of the Kelly optimum to buy (bots.toml [desk] kelly, else 0.5)")
    dk.add_argument("--min-edge", dest="min_edge", type=float, default=None,
                    help="the return on cost after both fees every contract must keep (bots.toml [desk] min_edge, else 0.02)")
    dk.add_argument("--reserve", type=float, default=None, help="the share of the bankroll kept out of the market (else 0.10)")
    dk.add_argument("--max-hours", dest="max_hours", type=float, default=None,
                    help="closes further out are watched, not traded (else 720)")
    dk.add_argument("--live", action="store_true", help=f"trade real sats (asks you to type LIVE, or set {LIVE_ENV}=I_UNDERSTAND)")
    dk.add_argument("--once", action="store_true", help="one pass over every close, then exit")
    dk.add_argument("--manage", action="store_true",
                    help="trade every position your account holds in these series, not only what the desk opened (needs an API key)")
    hp = sub.add_parser("hashprice", help="the bench the hashprice forecast is built on")
    hp.add_argument("what", nargs="?", choices=("now", "backtest", "fit"), default="now",
                    help="now: today's forecast of every close against the market; backtest: against the baselines on past closes; "
                         "fit: search the parameters on past closes and report on later ones")
    hp.add_argument("--save", action="store_true", help="fit: keep what it chose, for the bot to use")
    hp.add_argument("--paths", type=int, default=1500, help="simulated paths a forecast in the backtest")
    df = sub.add_parser("difficulty", help="the bench the difficulty and adjustment forecasts are built on")
    df.add_argument("what", nargs="?", choices=("now", "backtest", "fit", "hedge"), default="now",
                    help="now: the coming retargets against the market; backtest: against the baselines on past retargets; fit: search "
                         "the parameters on past retargets and report on later ones; hedge: what leaning against miners would have done")
    df.add_argument("--save", action="store_true", help="fit: keep what it chose, for the bots to use")
    df.add_argument("--paths", type=int, default=1500, help="simulated paths a forecast in the backtest")
    args = p.parse_args(argv)
    if args.cmd == "bots":
        return list_bots(args.search)
    if args.cmd == "hashprice":
        from .hashprice_lab import main as bench

        return bench(args.what, args.save, args.paths)
    if args.cmd == "difficulty":
        from .difficulty_lab import main as bench

        return bench(args.what, args.save, args.paths)
    if args.cmd == "about":
        return about_bot(args.bot)
    try:
        return asyncio.run(desk(args) if args.cmd == "desk" else run(args))
    except KeyboardInterrupt:
        return 0
