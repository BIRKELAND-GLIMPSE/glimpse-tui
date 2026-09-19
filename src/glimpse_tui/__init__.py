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

    Terminal().run()
