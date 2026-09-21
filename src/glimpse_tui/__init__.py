"""Glimpse terminal: the prediction market, in your terminal."""
from __future__ import annotations

import sys

__version__ = "0.1.0"


def main() -> None:
    if sys.argv[1:2] and sys.argv[1] in ("bots", "about", "run"):
        from .cli import main as cli_main

        raise SystemExit(cli_main(sys.argv[1:]))
    if any(a in ("-V", "--version") for a in sys.argv[1:]):
        print(f"glimpse-tui {__version__}")
        return
    if any(a in ("-h", "--help") for a in sys.argv[1:]):
        from .app import HELP

        print("glimpse-tui: the Glimpse prediction market in your terminal.\n\n" + HELP)
        print("\n Without the screen:  glimpse-tui bots [search]   glimpse-tui about <bot>\n"
              "                      glimpse-tui run <bot> [--series BTC] [--budget 20000] [--live] [--once]")
        return
    import os

    from . import auth
    from .heatmap import wants_truecolor

    # Textual reads this once, at import: decide the colour depth before it loads, so a terminal that
    # can draw 24-bit colour but does not advertise it (or the reverse) still gets the right palette.
    os.environ.setdefault("TEXTUAL_COLOR_SYSTEM", "truecolor" if wants_truecolor(saved=auth.load_state().get("colors", "")) else "256")
    from .app import Terminal
    from .term import config

    words = [a for a in sys.argv[1:] if not a.startswith("-")]
    Terminal(launchpad=opening_launchpad(sys.argv[1:], config.load()), go=" ".join(words)).run()


def opening_launchpad(argv: list[str], cfg: dict) -> str | None:
    """Which screen a bare `glimpse-tui` opens on, as a launchpad name or None for the odds.

    The odds on Bitcoin's next hour is the default: the simplest thing the terminal does and the one you can act
    on. The front page is behind `t`, `--terminal`, or `opens_on = "terminal"` in terminal.toml. `--odds` (and
    `--markets`, which meant this before) forces the odds back.
    """
    where = str(cfg.get("opens_on") or "odds").lower()
    if any(f in argv for f in ("--terminal", "--front", "-t")):
        where = "terminal"
    if any(f in argv for f in ("--odds", "--markets")):
        where = "odds"
    return (cfg.get("default_launchpad") or "BTC") if where == "terminal" else None
