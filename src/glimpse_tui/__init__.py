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

    # `glimpse-tui` opens the default launchpad. `glimpse-tui MEMP`, `glimpse-tui MSTR DES` or `glimpse-tui LP MACRO` opens
    # on that GO bar command. `glimpse-tui --markets` opens on the markets and ladder, as the terminal did before launchpads.
    words = [a for a in sys.argv[1:] if not a.startswith("-")]
    launchpad = None if "--markets" in sys.argv[1:] else (config.load().get("default_launchpad") or "BTC")
    Terminal(launchpad=launchpad, go=" ".join(words)).run()
