"""The bet slip, on the right like the web app's trade sidebar.

Top to bottom: the market's probability that your prediction comes true, the prediction you can edit, the positions
that prediction works out to across several closes, then what it costs and returns (cost, payout, profit, odds, ROI,
all in the same plain bold text), then the button, pinned to the bottom of the slip. Nothing else.
"""
from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING

from rich.text import Text
from textual.widget import Widget

from . import fmt
from . import pricing as P
from .theme import BURNT, DIM, FAINT, GREEN, ORANGE, RED, RULE, TEXT

if TYPE_CHECKING:
    from .app import Terminal

WIDTH = 42          # the slip, whatever the order touches: the legs list is cut to fit rather than widening it
LEGS_ROWS = 9       # legs listed before the list scrolls around the one under the cursor


class SlipPane(Widget):
    can_focus = False

    @property
    def t(self) -> Terminal:
        return self.app  # type: ignore[return-value]

    def render(self) -> Text:
        t, w = self.t, max(self.size.width, 24)
        out = Text(no_wrap=True, overflow="ellipsis")
        tk = t.ticket

        def row(label: str, value: str, colour: str) -> None:
            out.append(f" {label}", style=f"bold {colour}")
            out.append(value.rjust(w - len(label) - 2) + "\n", style=f"bold {colour}")

        def rule(title: str = "") -> None:
            out.append(f" {title} ".ljust(w - 1, "─") + "\n" if title else " " + "─" * (w - 2) + "\n", style=RULE if not title else FAINT)

        out.append(f" {t.slip_title}\n", style=f"bold {TEXT}")
        for line in t.slip_when:
            out.append(f" {line}\n", style=DIM)

        probs = t.sel_probs
        if probs:
            every, anyone, expect = P.chances(probs)
            rule("MARKET PROBABILITY")
            n = len(probs)
            row(f"ALL {n} LAND" if n > 1 else "LANDS IN RANGE", fmt.pct(every), ORANGE)
            out.append(" " + fmt.bar(every, w - 3).ljust(w - 3) + "\n", style=f"{ORANGE} on #262626")
            if n > 1:
                row("AT LEAST ONE", fmt.pct(anyone), TEXT)
                row("EXPECTED TO LAND", f"{expect:.1f} of {n}", TEXT)
                row("EACH CLOSE", f"{fmt.pct(min(probs))} – {fmt.pct(max(probs))}", DIM)
                out.append(" closes treated as independent\n", style=FAINT)

        fields = t.slip_fields()
        if fields:
            rule("YOUR PREDICTION")
            vw = max(len(f[2]) for f in fields)
            for i, (_, label, value) in enumerate(fields):
                on = t.slip_focus and i == t.slip_cur
                line = f" {'▸' if on else ' '} {label:<6}" + f"‹  {value:^{vw}}  ›".rjust(w - 10)
                out.append(line.ljust(w) + "\n", style=f"bold {ORANGE} on {BURNT}" if on else f"bold {TEXT}")

        legs = t.legs
        if len(legs) > 1:
            rule(f"{len(legs)} POSITIONS")
            cw = (w - 14) // 3          # the close label takes 12 at the slip's width, which is exactly what the
            rw, lw = cw + 1, w - 3 * cw - 3     # longest one needs; roi gets the spare column, being the widest number

            out.append(f" {'close':<{lw}}{'cost':>{cw}}{'payout':>{cw}}{'roi':>{rw}}"[:w] + "\n", style=FAINT)
            start = max(0, min(t.leg_cur - LEGS_ROWS // 2, len(legs) - LEGS_ROWS))
            for i, leg in enumerate(legs[start:start + LEGS_ROWS], start):
                lt, on = leg.ticket, i == t.leg_cur      # `lt`, not `tk`: `tk` below is the whole order's ticket
                mood = GREEN if lt.profit_sats > 0 else RED
                line = Text(no_wrap=True, overflow="crop")
                line.append(f" {'▸' if on else ' '}{fmt.close_label(leg.end_time, t.hm_cadence < 86400):<{lw - 1}}",
                            style=f"bold {ORANGE}" if on else TEXT)
                line.append(f"{fmt.sats(lt.cost_sats):>{cw}}", style=TEXT)
                line.append(f"{fmt.sats(lt.payout_sats):>{cw}}", style=ORANGE)
                line.append(f"{fmt.roi(lt.roi):>{rw}}", style=mood)
                out.append(line)
                out.append("\n")
            if len(legs) > LEGS_ROWS:
                out.append(f" {start + 1}–{min(start + LEGS_ROWS, len(legs))} of {len(legs)}\n", style=FAINT)

        if tk is None:
            out.append(f"\n {t.ticket_hint or 'Move onto a price range to price it.'}\n", style=DIM)
            return out

        top, out = out, Text(no_wrap=True, overflow="ellipsis")     # the ticket and its button sit on the slip's bottom edge
        multi = tk.markets > 1
        good = GREEN if tk.roi > 0 else RED
        rule("TICKET")
        row("COST", fmt.sats(tk.cost_sats), TEXT)
        row("MAX PAYOUT" if multi else "PAYOUT", fmt.sats(tk.payout_sats), ORANGE)
        row("MAX PROFIT" if multi else "PROFIT", fmt.sats(tk.profit_sats, signed=True), ORANGE if tk.profit_sats > 0 else RED)
        row("AVG ODDS" if multi else "ODDS", fmt.odds(tk.odds), good)
        row("AVG ROI" if multi else "ROI", fmt.roi(tk.roi), good)
        out.append("\n")

        note, style = "", DIM
        if not t.ticket_live:
            note, style = "Market closed, awaiting settlement.", RED
        elif tk.profit_sats <= 0:
            note, style = "Costs more than it can pay: narrow the range.", RED
        elif t.wallet and tk.cost_sats > t.wallet.balance_sats:
            note, style = f"Exceeds your balance of {fmt.sats(t.wallet.balance_sats)}.", RED
        elif multi:
            note = f"Each close pays {fmt.sats(tk.payout_sats / tk.markets)} if it lands in range."
        if note:
            for line in textwrap.wrap(note, w - 2):
                out.append(f" {line}\n", style=style)
            out.append("\n")

        if not t.api.authenticated:
            out.append(" L  log in to trade ".center(w), style=f"bold {TEXT} on #2a2a2a")
        elif style == RED:
            out.append(" not tradable as it stands ".center(w), style=f"{DIM} on #2a2a2a")
        else:
            out.append(f" b  Bet {fmt.sats(tk.cost_sats)} → Win {fmt.sats(tk.payout_sats)} ".center(w), style=f"bold #000000 on {ORANGE}")
        gap = self.size.height - top.plain.count("\n") - out.plain.count("\n") - 1
        top.append("\n" * max(gap, 1))
        return top + out
