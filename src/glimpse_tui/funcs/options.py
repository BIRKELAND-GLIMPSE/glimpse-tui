"""OMON and GIV: every Glimpse close read as an options chain (TERMINAL.md 8).

A close is a probability distribution over price bins. Summing it above a strike gives the price of a digital call,
below it a digital put, between two strikes a range. A lognormal fitted to the distribution gives an implied
volatility per close, a term structure across closes, and the Greeks of each digital under that fit.

Nothing here places an order. Enter hands a range to the heatmap as a box, and the bet slip, the Confirm dialog and
the server-estimate gate take it from there, unchanged.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass

from rich.text import Text

from .. import charts, fmt
from .. import pricing as P
from ..data.btcmath import YEAR_S
from ..data.core import LIVE, Provenance, SourceError
from ..term import ui
from ..term.panes import FuncPane
from ..term.registry import Function, register
from ..theme import DIM, FAINT, ORANGE, TEXT

SQRT2PI = math.sqrt(2 * math.pi)


def ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def npdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / SQRT2PI


@dataclass
class Fit:
    """ln(price at the close) read as normal(m, s): the market's own distribution, summarised."""
    m: float
    s: float
    t: float                    # seconds to the close

    @property
    def iv(self) -> float:
        return self.s * math.sqrt(YEAR_S / self.t) if self.t > 0 else 0.0

    def above(self, k: float) -> float:
        return ncdf((self.m - math.log(k)) / self.s) if self.s > 0 and k > 0 else 0.0

    def greeks(self, k: float, spot: float) -> tuple[float, float, float, float]:
        """Of the digital call struck at k: delta per $1,000 of spot, gamma per ($1,000)², vega per volatility point,
        theta per hour. A move in spot shifts the whole lognormal: dm = dS / S."""
        if not (self.s > 0 and k > 0 and spot > 0 and self.t > 0):
            return 0.0, 0.0, 0.0, 0.0
        d = (self.m - math.log(k)) / self.s
        pdf = npdf(d)
        delta = pdf / (self.s * spot)
        gamma = -pdf * (d / self.s + 1.0) / (self.s * spot * spot)
        ds_dvol = math.sqrt(self.t / YEAR_S)                        # ds per unit of annualised volatility
        vega = -pdf * d / self.s * ds_dvol                          # m held fixed: the median stays where the market put it
        theta = -(-pdf * d / self.s) * (self.s / (2 * self.t))      # s shrinks like sqrt(t): ds/dt = s / 2t, and time runs down
        return delta * 1000, gamma * 1e6, vega / 100, theta * 3600


def distribution(view) -> tuple[list[tuple[float, float]], list[float]]:
    """(bins, probabilities) for one close, with the market maker's seed floor removed, as the heatmap shows it (PLAN D9)."""
    sig = P.signal(P.implied_probs([r * 100 for r in view.raw]))
    total = sum(sig) or 1.0
    return list(view.bins), [x / total for x in sig]


def fit(bins: list[tuple[float, float]], probs: list[float], seconds: float) -> Fit | None:
    pts = [(math.log((lo + hi) / 2), p) for (lo, hi), p in zip(bins, probs, strict=True) if p > 0 and lo + hi > 0]
    if len(pts) < 2 or seconds <= 0:
        return None
    m = sum(x * p for x, p in pts)
    var = sum(p * (x - m) ** 2 for x, p in pts)
    width = math.log(bins[0][1] / bins[0][0]) if bins[0][0] > 0 else 0.0
    s = math.sqrt(max(var - width * width / 12, 1e-12))             # Sheppard's correction: the bins are not points
    return Fit(m, s, seconds)


def above(bins: list[tuple[float, float]], probs: list[float], k: float) -> float:
    """P(close ≥ k) straight from the bins, for a strike on a bin edge."""
    return sum(p for (lo, _), p in zip(bins, probs, strict=True) if lo >= k - 1e-9)


def strikes(bins: list[tuple[float, float]], probs: list[float], n: int) -> list[int]:
    """About `n` strikes on bin edges across the part of the ladder that carries the probability, evenly spaced on round steps."""
    live = [i for i, p in enumerate(probs) if p > 1e-4]
    if not live:
        return []
    lo, hi = live[0], live[-1] + 1
    step = max(math.ceil((hi - lo) / max(n - 1, 1)), 1)
    first = lo - lo % step
    return [i for i in range(first, min(hi + step, len(bins)), step)]


class OmonPane(FuncPane):
    code, every, selectable, tick = "OMON", 20, True, 5.0

    def __init__(self, hub, security=None, args=()) -> None:
        super().__init__(hub, security, args)
        self.close_i = 0
        self.deribit: tuple[float, str, Provenance] | None = None
        self._ks: list[int] = []

    async def load(self) -> None:
        self.title = "options on every close"
        if d := self.hub.sources.get("deribit"):
            try:
                from ..data.deribit import atm_vols
                rows, prov = await d.book_summary("option")
                near = atm_vols(rows, n=1)
                self.deribit = (near[0].iv / 100, near[0].label, prov) if near else None
            except (SourceError, AttributeError):
                self.deribit = None

    def _views(self) -> list:
        return list(self.hub.account().get("views") or [])

    def cache_key(self) -> tuple:
        v = self._views()
        return (self.close_i, len(v), v[self.close_i].median if v and self.close_i < len(v) else 0, self.hub.account().get("asset"))

    def draw(self, w: int, h: int) -> list[Text]:
        views, acct, now = self._views(), self.hub.account(), time.time()
        if not views:
            return ui.centre(["Glimpse's closes have not loaded yet.", "They arrive from the public Glimpse API within a few seconds."], w, h)
        self.close_i = max(0, min(self.close_i, len(views) - 1))
        v = views[self.close_i]
        bins, probs = distribution(v)
        secs = v.row.end_time_utc - now
        f = fit(bins, probs, secs)
        spot = self.hub.btc_price() if acct.get("asset") == "BTC" else None
        self.provs = [Provenance("glimpse", now, now, LIVE)] + ([self.deribit[2]] if self.deribit else [])
        q = fmt.question(str(acct.get("asset", "")), int(v.row.end_time_utc))
        head = ui.fit([(f"{q}", f"bold {TEXT}"), (f"  closes in {fmt.countdown(int(v.row.end_time_utc))}", DIM),
                       (f"   median {ui.px(v.median, 0)}", ui.CYAN),
                       (f"   80% {ui.px(v.band[0], 0)} – {ui.px(v.band[1], 0)}", DIM)], w)
        iv = ui.fit([("implied vol ", FAINT), (f"{f.iv:.1%}" if f else "–", f"bold {ORANGE}"), (" annualised, lognormal fit", FAINT),
                     *(((f"   Deribit {self.deribit[1]} ATM ", FAINT), (f"{self.deribit[0]:.1%}", f"bold {TEXT}")) if self.deribit else ()),
                     (f"   close {self.close_i + 1} of {len(views)}", FAINT)], w)
        ks = self._ks = strikes(bins, probs, max(h - 5, 6))
        self.n_rows = len(ks)
        self.cur = max(0, min(self.cur, len(ks) - 1))
        wide = w >= 96
        rows = []
        for n, i in enumerate(ks):
            k = bins[i][0]
            pc = above(bins, probs, k)
            nxt = bins[ks[n + 1]][0] if n + 1 < len(ks) else bins[-1][1]
            rng = pc - above(bins, probs, nxt)
            d, g, vg, th = f.greeks(k, spot or v.median) if f else (0, 0, 0, 0)
            atm = spot is not None and k <= spot < nxt
            row = [Text(f"{'◂' if atm else ' '}{ui.px(k, 0)}", style=f"bold {ORANGE if atm else TEXT}"),
                   f"{pc * 100:5.1f}", f"{(1 - pc) * 100:5.1f}",
                   Text(charts.hbar(rng / max(max(probs) * (ks[1] - ks[0] if len(ks) > 1 else 1), 1e-9), 12), style=charts.snap(ORANGE)),
                   f"{rng * 100:5.1f}", f"{(1 / pc if pc > 0.001 else 0):,.2f}×" if pc > 0.001 else "–"]
            if wide:
                row += [f"{d:+.3f}", f"{g:+.4f}", f"{vg:+.4f}", f"{th:+.4f}"]
            rows.append(row)
        cols = [ui.Col("strike"), ui.Col("call ₿"), ui.Col("put ₿"), ui.Col("range to next", "left"),
                ui.Col("rng ₿"), ui.Col("call odds", style=DIM)]
        if wide:
            cols += [ui.Col("Δ /$1k", style=DIM), ui.Col("Γ", style=DIM), ui.Col("vega /pt", style=DIM), ui.Col("θ /h", style=DIM)]
        table = ui.table(cols, list(reversed(rows)), w, self.cur if rows else None)     # highest strike on top, like the ladder
        self.follow(4 + self.cur, h)
        note = ui.fit([("prices are sats per 100-sat payout, before the 2% fee · ", FAINT), ("enter", f"bold {ORANGE}"),
                       (" range → heatmap  ", DIM), ("C", f"bold {ORANGE}"), (" call  ", DIM), ("U", f"bold {ORANGE}"), (" put", DIM)], w)
        return [head, iv, *table, note]

    def _handoff(self, lo_bin: int, hi_bin: int) -> None:
        self.hub.handoff(self.close_i, lo_bin, hi_bin)

    def _at(self) -> int:
        """Index into the strikes for the cursor row: the table is drawn highest strike first."""
        return len(self._ks) - 1 - self.cur

    def key(self, k: str, ch: str | None) -> bool:
        views = self._views()
        if ch in ("[", "]", "{", "}"):
            step = (1 if ch in "]}" else -1) * (24 if ch in "{}" else 1)
            self.close_i = max(0, min(self.close_i + step, max(len(views) - 1, 0)))
            return True
        if ch in ("C", "U") and self._ks and views:
            bins, probs = distribution(views[self.close_i])
            live = [i for i, p in enumerate(probs) if p > 1e-4]
            k = self._ks[self._at()]
            if ch == "C":
                self._handoff(k, max(live[-1], k))                  # the digital call: every range from the strike up
            elif k > live[0]:
                self._handoff(live[0], k - 1)                       # the digital put: every range below the strike
            return True
        return False

    def enter(self) -> str | None:
        if self._ks:
            at = self._at()
            k = self._ks[at]
            nxt = self._ks[at + 1] if at + 1 < len(self._ks) else k + 1
            self._handoff(k, nxt - 1)
        return None

    def hint(self) -> str:
        return "[ ] close · { } a day · enter range to the heatmap · C call · U put"

    def export(self):
        views = self._views()
        if not views:
            return None
        bins, probs = distribution(views[self.close_i])
        return ["strike", "p_above", "p_below"], [[bins[i][0], above(bins, probs, bins[i][0]), 1 - above(bins, probs, bins[i][0])]
                                                  for i in self._ks]


def term_structure(views: list, now: float) -> list[tuple[float, float]]:
    """(hours to close, annualised implied volatility) for every close that fits."""
    out = []
    for v in views:
        secs = v.row.end_time_utc - now
        if secs < 600:
            continue
        bins, probs = distribution(v)
        if f := fit(bins, probs, secs):
            out.append((secs / 3600, f.iv))
    return out


class GivPane(FuncPane):
    code, every, tick = "GIV", 60, 10.0

    def __init__(self, hub, security=None, args=()) -> None:
        super().__init__(hub, security, args)
        self.dvol: tuple[float, Provenance] | None = None
        self.curve: list[tuple[float, float]] = []

    async def load(self) -> None:
        self.title = "Glimpse implied volatility against Deribit's DVOL"
        views = list(self.hub.account().get("views") or [])
        import asyncio
        self.curve = await asyncio.to_thread(term_structure, views, time.time())     # 170 closes × 500 bins: off the loop
        if d := self.hub.sources.get("deribit"):
            try:
                from ..data.deribit import window
                rows, prov = await d.dvol("3600", *window(2))
                self.dvol = (float(rows[-1][4]) / 100, prov) if rows else None
            except (SourceError, AttributeError):
                self.dvol = None

    def draw(self, w: int, h: int) -> list[Text]:
        now = time.time()
        self.provs = [Provenance("glimpse", now, now, LIVE)] + ([self.dvol[1]] if self.dvol else [])
        if not self.curve:
            return ui.centre(["Glimpse's closes have not loaded yet."], w, h)
        near, far = self.curve[0], self.curve[-1]
        day = min(self.curve, key=lambda c: abs(c[0] - 24))
        head = ui.fit([("nearest ", FAINT), (f"{near[1]:.1%}", f"bold {ORANGE}"), (f" ({near[0]:.1f}h)", DIM), ("   24h ", FAINT),
                       (f"{day[1]:.1%}", f"bold {TEXT}"), (f"   {far[0] / 24:.0f}d ", FAINT), (f"{far[1]:.1%}", f"bold {TEXT}"),
                       *((("   Deribit DVOL ", FAINT), (f"{self.dvol[0]:.1%}", f"bold {ui.CYAN}"), (" (30-day)", FAINT)) if self.dvol else
                         (("   Deribit DVOL unavailable", FAINT),))], w)
        plot = charts.Plot(w, max(h - 3, 4), times=False)
        plot.fmt = lambda v: f"{v:.0%}"
        plot.line([c[0] for c in self.curve], [c[1] for c in self.curve], ORANGE, label="Glimpse IV by hours to close")
        if self.dvol:
            plot.hline(self.dvol[0], ui.CYAN, f"DVOL {self.dvol[0]:.0%}", glyph="┄")
        body = plot.render().split("\n")
        body[-1] = ui.fit([(f"{near[0]:.0f}h", FAINT), (" " * max(w - 24, 1), ""), (f"{far[0]:.0f}h to close", FAINT)], w)
        spread = (day[1] - self.dvol[0]) if self.dvol else None
        tail = ui.fit([("Glimpse 24h minus DVOL ", FAINT), ui.signed_pct(spread) if spread is not None else Text("–"),
                       ("   a close's volatility is its distribution's width, read as a lognormal and annualised", FAINT)], w)
        return [head, *body, tail]

    def export(self):
        return ["hours_to_close", "implied_vol"], [[round(a, 2), round(b, 4)] for a, b in self.curve]


_GL = ("Reads the closes the terminal has loaded from the public Glimpse API (the series the odds screen is on, hourly BTC by "
       "default), refreshed "
       "with them every 60 s. With no closes loaded the page says so.")
register(Function(
    "OMON", "Option monitor", "Glimpse", "an options chain read off each close: digital calls, puts, ranges, IV, Greeks", OmonPane,
    needs=("glimpse closes", "deribit"),
    help="For the chosen close, every strike on a bin edge: the digital call price P(close at or above K) and the digital put price, "
         "each in sats per 100-sat payout before the 2% fee, the price of the range up to the next strike with a bar, and the call's "
         "decimal odds. The implied volatility is the width of a lognormal fitted to the close's distribution (the market maker's "
         "seed floor removed, with Sheppard's correction for the bin width), annualised. On a wide pane the Greeks of each digital "
         "under that fit: delta per $1,000 of spot, gamma, vega per volatility point, theta per hour. Deribit's at-the-money implied "
         "volatility for its nearest expiry sits beside it. [ and ] step through closes, { and } a day at a time. Enter hands the "
         "range from the strike to the next one to the heatmap as a box; C hands the call (every range above), U the put. The "
         "heatmap's bet slip, confirmation and server-estimate gate then apply exactly as always: OMON itself never places an order. " + _GL))
register(Function(
    "GIV", "Glimpse implied volatility", "Glimpse", "Glimpse's IV term structure against Deribit's DVOL", GivPane,
    needs=("glimpse closes", "deribit"),
    help="One point per close: hours to the close against the annualised volatility implied by that close's distribution, the same "
         "fit OMON uses. Deribit's DVOL index, a 30-day implied volatility from its option book, is the cyan rule. The line below "
         "gives the gap between Glimpse's 24-hour volatility and DVOL. Deribit is read from its public API once a minute; when it "
         "is down the rule is absent and the page says so. " + _GL))

