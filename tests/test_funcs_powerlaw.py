"""PL: the power law. The regression itself, the pane on the recorded sources, and the ratio instruments it needs. No network."""
import math
import random

import pytest
from rich.cells import cell_len

from glimpse_tui import fmt
from glimpse_tui.data import core, quotes
from glimpse_tui.data.core import Provenance
from glimpse_tui.funcs import powerlaw as PL
from glimpse_tui.term import instruments, registry
from glimpse_tui.term.hub import Hub, Quote

SIZES = ((36, 11), (44, 19), (60, 19), (78, 19), (120, 34), (196, 50))


def make_hub() -> Hub:
    hub = Hub()
    for s in hub.sources.values():
        s.retries, s.bucket = 0, core.TokenBucket(1000, 50)
    return hub


async def pane(ticker: str = "BTC", args=()) -> PL.PlPane:
    hub = make_hub()
    p = PL.PlPane(hub, hub.book.get(ticker), args)
    p.securities = (hub.book.get(ticker),)
    await p.reload()
    return p


# ── the regression ──────────────────────────────────────────

def test_the_fit_recovers_a_power_law_it_is_given():
    """A series built to be 10^-17.3 · days^5.8 with lognormal noise must come back with those numbers."""
    random.seed(7)
    ts, ps = [], []
    for i in range(5000):
        t = PL.GENESIS + (400 + i) * PL.DAY
        ts.append(t)
        ps.append(10 ** -17.3 * PL.days_of(t) ** 5.8 * 10 ** random.gauss(0, 0.22))
    f = PL.fit(ts, ps)
    assert abs(f.n - 5.8) < 0.05 and abs(f.a + 17.3) < 0.3
    assert f.r2 > 0.97 and abs(f.sd - 0.22) < 0.02 and f.points == 5000
    assert abs(f.at(ts[-1]) / (10 ** -17.3 * PL.days_of(ts[-1]) ** 5.8) - 1) < 0.1


def test_a_fit_needs_years_of_closes_and_says_so_by_returning_nothing():
    ts = [PL.GENESIS + (400 + i) * PL.DAY for i in range(PL.MIN_POINTS - 1)]
    assert PL.fit(ts, [100.0 + i for i in range(len(ts))]) is None
    assert PL.fit(ts + [ts[-1] + PL.DAY], [100.0] * (len(ts) + 1)) is not None       # flat, but enough of it


def test_a_fit_needs_the_closes_spread_along_the_axis_not_just_many_of_them():
    """A year of daily closes at day 6,000 covers a hundredth of a decade: the slope through it is noise."""
    ts = [PL.GENESIS + (6000 + i) * PL.DAY for i in range(365)]
    assert PL.fit(ts, [50_000.0 + 100 * i for i in range(len(ts))]) is None
    long = [PL.GENESIS + (1000 + i) * PL.DAY for i in range(5000)]
    assert PL.fit(long, [10.0 * (1 + i) for i in range(len(long))]) is not None


def test_the_band_and_sigmas_are_two_readings_of_the_same_residual():
    random.seed(3)
    ts = [PL.GENESIS + (400 + i) * PL.DAY for i in range(1200)]
    ps = [10 ** -8 * PL.days_of(t) ** 3 * 10 ** random.gauss(0, 0.2) for t in ts]
    f = PL.fit(ts, ps)
    t = ts[-1]
    assert f.sigmas(t, f.band(t, 1.0)) == pytest.approx(1.0)
    assert f.sigmas(t, f.band(t, -2.0)) == pytest.approx(-2.0)
    assert f.band(t, 1) > f.at(t) > f.band(t, -1)


def test_a_falling_series_fits_a_negative_exponent():
    ts = [PL.GENESIS + (400 + i) * PL.DAY for i in range(1000)]
    ps = [10 ** 12 * PL.days_of(t) ** -3.5 for t in ts]
    f = PL.fit(ts, ps)
    assert f.n == pytest.approx(-3.5, abs=0.01)
    halves = f.doubles_in()                                 # a falling line halves; the pace is still a number
    assert halves and abs(f.at(f.last + halves * PL.DAY) / f.at(f.last) - 0.5) < 0.02


def test_a_negative_exponent_carries_the_house_minus_sign():
    assert PL.signed(-3.633) == "−3.633" and PL.signed(5.624) == "5.624" and PL.signed(-3.6, 2) == "−3.60"


def test_the_year_marks_thin_out_to_the_room_they_have():
    lo, hi = PL.year_start(2011), PL.year_start(2026)
    wide = PL.year_marks(lo, hi, 200)
    narrow = PL.year_marks(lo, hi, 12)
    assert [lab for _, lab in wide][:3] == ["'11", "'12", "'13"] and len(narrow) < len(wide)
    assert all(PL.at_days(d) >= lo for d, _ in wide)


# ── the instruments it prices in Bitcoin ────────────────────

def test_any_ticker_the_book_knows_has_a_bitcoin_form_except_a_yield():
    book = instruments.load()
    gold = book.get("XAUBTC")
    assert gold is not None and gold.quote == "BTC" and instruments.leg_of(gold) == "XAU"
    assert book.get("XAU/BTC") is gold or book.get("XAU/BTC").ticker == "XAUBTC"
    assert book.get("SPXBTC") is not None and book.get("NVDABTC") is not None
    assert book.get("US10YBTC") is None                  # a yield is a percentage: there is nothing to divide
    assert book.get("BTCBTC") is None
    assert book.get("FBTC").ticker == "FBTC"             # a real ticker that happens to end in BTC is itself


def test_a_ratio_quote_divides_the_two_legs_into_satoshis():
    hub = Hub()
    now = 1_700_000_000.0
    hub.quotes["XAU"] = Quote("XAU", 4000.0, Provenance("yahoo", now, now, "delayed"), prev=3900.0)
    hub.quotes["BTC"] = Quote("BTC", 100_000.0, Provenance("coinbase", now, now, "live"), prev=90_000.0)
    q = quotes.ratio_quote(hub, hub.book.get("XAUBTC"), "XAU")
    assert q.price == pytest.approx(4_000_000)                       # 0.04 BTC an ounce
    assert q.prev == pytest.approx(3900 / 90_000 * 1e8)
    assert q.pct < 0                                                 # gold fell against Bitcoin even as both rose
    assert q.prov.source == "computed:ratio" and q.prov.delay == "delayed"       # the slower leg governs
    assert q.history == ()                                           # never the numerator's shape scaled by today's price


def test_a_ratio_history_uses_only_the_days_both_legs_closed():
    day = 86400.0
    a = ([day * 10, day * 11, day * 12, day * 14], [100.0, 110.0, 120.0, 140.0])
    b = ([day * 10, day * 12, day * 13, day * 14], [10.0, 12.0, 13.0, 14.0])
    ts, num, den = quotes.align(a, b)
    assert ts == [day * 10, day * 12, day * 14] and num == [100.0, 120.0, 140.0] and den == [10.0, 12.0, 14.0]


def test_a_price_in_bitcoin_reads_as_satoshis_until_it_is_a_whole_coin():
    assert fmt.in_btc(5_456_802) == "₿5,456,802"
    assert fmt.in_btc(250_000_000) == "₿2.5000"
    assert fmt.in_btc(903.4) == "₿903.40"
    assert fmt.in_btc(-12_345, signed=True) == "−₿12,345"
    assert fmt.in_btc(None) == "–"


# ── the pane, on the recorded sources ───────────────────────

async def test_bitcoin_fits_its_own_power_law_on_the_whole_recorded_history(no_network):
    p = await pane("BTC")
    assert not p.error, p.error
    assert len(p.ts) > 5000 and p.ts[0] < PL.year_start(2011)        # the chain price, not 300 days of exchange candles
    f = p.fitted()
    assert 4.0 < f.n < 7.0 and f.r2 > 0.9                            # the shape everyone means by "the power law"
    body = "\n".join(ln.plain for ln in p.draw(120, 34))
    assert "exponent" in body and "R²" in body and PL.signed(f.n) in body


async def test_bitcoin_history_past_a_year_comes_off_the_chain_not_an_exchange(no_network):
    hub = make_hub()
    long_, short = await quotes.history(hub, hub.book.get("BTC"), 9000), await quotes.history(hub, hub.book.get("BTC"), 200)
    assert long_[2].source == "bitview.space" and len(long_[0]) > 5000
    assert short[2].source != "bitview.space" and len(short[0]) <= 200


async def test_the_page_fits_every_pane_size_and_never_runs_past_its_width(no_network):
    p = await pane("BTC")
    for w, h in SIZES:
        for table in (False, True):
            p.table = table
            lines = p.draw(w, h)
            assert lines, (w, h, table)
            assert not [ln.plain for ln in lines if cell_len(ln.plain) > w], (w, h, table)


async def test_the_windows_offered_are_the_ones_the_history_reaches_back_past(no_network):
    p = await pane("BTC")
    names = [n for n, _ in p.windows()]
    assert names[0] == "all" and "2013" in names                      # Bitcoin's own history reaches 2010
    p.ts = [PL.year_start(2020) + i * PL.DAY for i in range(1000)]
    assert [n for n, _ in p.windows()] == ["all"]                     # ten years of data offers no 2013 start
    assert p.window == ("all", 0)


async def test_moving_the_start_of_the_fit_moves_the_exponent_and_the_command_says_which(no_network):
    p = await pane("BTC")
    first = p.fitted().n
    assert p.key("]", "]") and p.win != "all"
    assert p.command().endswith(p.win) and p.window[0] == p.win
    assert p.fitted().n != first                                      # a different start is a different line
    assert p.key("T", "T") and p.table and "TABLE" in p.command()


async def test_a_year_of_gold_against_bitcoin_is_refused_rather_than_fitted(no_network):
    """With Yahoo off, gold reaches the terminal through PAXG, whose recorded history is one year. One year at
    Bitcoin's age is a hundredth of a decade on the x axis, so the page says so instead of printing a slope."""
    p = await pane("XAUBTC")
    assert p.error and "too little history" in p.error
    assert "XAUBTC" in p.error and "years" in p.error


@pytest.mark.yahoo
async def test_gold_priced_in_bitcoin_is_a_falling_power_law(no_network):
    """Ten years of COMEX gold against the chain's own Bitcoin price: a troy ounce costs fewer satoshis every year,
    which is the same law read from the other side, with a negative exponent."""
    p = await pane("XAUBTC")
    assert not p.error, p.error
    f = p.fitted()
    assert p.sats and f.n < 0 and f.points > 2000
    assert p.ts[0] < PL.year_start(2018) and p.ps[0] > p.ps[-1]
    body = "\n".join(ln.plain for ln in p.draw(120, 30))
    assert "₿" in body and "POWER LAW" in body and "XAUBTC" in body


async def test_the_function_is_registered_with_a_help_page_that_names_its_sources(no_network):
    fn = registry.get("PL")
    assert fn is not None and fn.make is PL.PlPane and fn.optional
    assert "power law" in fn.summary or "log-log" in fn.summary
    assert "bitview" in fn.help.lower() and "genesis" in fn.help.lower() and "not a forecast" in fn.help.lower()


async def test_the_export_carries_the_residual_of_every_close(no_network):
    p = await pane("BTC")
    header, rows = p.export()
    assert header == ["date", "days_since_genesis", "price", "power_law_fit", "sigmas_from_fit"]
    assert len(rows) == len(p.ts) and all(len(r) == 5 for r in rows)
    assert max(abs(r[4]) for r in rows) < 10                          # residuals in sigmas, not raw prices


# ── the chart it draws on ───────────────────────────────────

def test_a_log_x_axis_gives_equal_ratios_equal_room():
    from glimpse_tui import charts
    plot = charts.Plot(80, 12, log=True, times=False, xlog=True)
    assert plot._xfrac(10, 1, 1000) == pytest.approx(1 / 3)
    assert plot._xfrac(100, 1, 1000) == pytest.approx(2 / 3)
    flat = charts.Plot(80, 12)
    assert flat._xfrac(10, 1, 1000) == pytest.approx(9 / 999)


def test_a_power_law_draws_as_a_straight_line_on_log_log_axes():
    """The point of the whole page: 10^a·d^n must come out as a line on log-log axes, and as a curve without them."""
    from glimpse_tui import charts

    def straightness(xlog: bool) -> float:
        xs = [float(d) for d in range(10, 10_000)]
        plot = charts.Plot(90, 24, log=True, times=False, xlog=xlog, grid=False)
        plot.line(xs, [1e-6 * x ** 4 for x in xs], charts.ORANGE)
        rows = list(plot.render().split("\n"))[:-1]
        by_col: dict[int, list[int]] = {}
        for r, ln in enumerate(rows):
            for c, ch in enumerate(ln.plain):
                if 0x2800 <= ord(ch) <= 0x28FF:         # braille only: the axis labels are not ink
                    by_col.setdefault(c, []).append(r)
        pts = [(c, sum(rs) / len(rs)) for c, rs in sorted(by_col.items())]      # one dot a column: the ink's middle
        n = len(pts)
        mx, my = sum(p[0] for p in pts) / n, sum(p[1] for p in pts) / n
        sxy = sum((x - mx) * (y - my) for x, y in pts)
        sxx, syy = sum((x - mx) ** 2 for x, _ in pts), sum((y - my) ** 2 for _, y in pts)
        return abs(sxy / math.sqrt(sxx * syy))

    assert straightness(xlog=True) > 0.999
    assert straightness(xlog=False) < 0.95              # the same numbers on a linear x axis bend away


def test_explicit_x_ticks_replace_the_date_axis():
    from glimpse_tui import charts
    plot = charts.Plot(80, 8, times=False, xlog=True)
    plot.line([1.0, 100.0], [1.0, 100.0], charts.ORANGE)
    plot.x_ticks = [(1.0, "'09"), (100.0, "'26")]
    axis = plot.render().split("\n")[-1].plain
    assert "'09" in axis and "'26" in axis and axis.index("'09") < axis.index("'26")


async def test_a_short_pane_keeps_the_inner_channel_and_drops_the_outer_one(no_network):
    """Four rules and a price line in seven rows is mush. The ±1σ pair stays; the ±2σ pair goes."""
    p = await pane("BTC")
    f = p.fitted()
    tall = [ln.plain for ln in p._chart(f, 100, 20, "$")]
    short = [ln.plain for ln in p._chart(f, 100, 8, "$")]
    braille = lambda rows: sum(1 for r in rows for ch in r if 0x2800 <= ord(ch) <= 0x28FF)     # noqa: E731
    assert braille(tall) > braille(short)
    assert len(short) <= 9 and all(len(r) <= 100 for r in short + tall)


# ── what is on screen, against what is fitted ───────────────

async def test_the_chart_opens_on_the_present_and_zooms_out_to_the_whole_fit(no_network):
    """A log x axis squeezes sixteen years into the right-hand quarter of the frame. The default is a close view
    of where the price is now; the fit behind it is still the whole history, and `<` opens the frame out."""
    p = await pane("BTC")
    assert p.zoom == PL.DEFAULT_ZOOM == "4y" and p.years == 4.0
    f = p.fitted()
    near, _ = p.drawn(f)
    assert (near[-1] - near[0]) / (365.25 * PL.DAY) == pytest.approx(4.0, abs=0.1)
    assert f.points == len(p.ts) > 5000                              # …while the fit still reads every close
    assert p.key("<", "<") and p.zoom == "10y"
    assert p.key("<", "<") and p.zoom == "all" and p.years == 0.0
    whole, _ = p.drawn(f)
    assert whole[0] == p.ts[0] and len(whole) == len(p.ts)
    assert not p.key("<", "<") or p.zoom == "all"                    # and stops there
    assert p.key(">", ">") and p.zoom == "10y"


async def test_the_zoom_frames_the_price_not_the_channel(no_network):
    """Two standard deviations of a log residual is a factor of four either way. Letting it set the scale put
    four years of price into two rows, so the y range comes from the price and the fitted line."""
    p = await pane("BTC")
    f = p.fitted()
    ts, ps = p.drawn(f)
    body = "\n".join(ln.plain for ln in p._chart(f, 120, 20, "$"))
    rows = [r for r in body.split("\n") if any(0x2800 <= ord(c) <= 0x28FF for c in r)]
    assert len(rows) >= 12                                           # the picture uses the frame it is given
    assert f.band(ts[-1], 2.0) > max(ps) or f.band(ts[-1], -2.0) < min(ps)   # the channel really is off the scale


async def test_the_zoom_survives_a_saved_launchpad_and_the_default_is_not_spelled_out(no_network):
    p = await pane("BTC")
    assert p.command() == "PL BTC all"                               # the default zoom adds nothing
    assert p.key("<", "<") and p.command() == "PL BTC all @10y"
    hub = p.hub
    again = PL.PlPane(hub, hub.book.get("BTC"), ("all", "@10y"))
    assert again.zoom == "10y"


async def test_the_chart_marks_where_the_price_is_now(no_network):
    p = await pane("BTC")
    body = "\n".join(ln.plain for ln in p.draw(120, 30))
    assert f"now {p.money(p.spot())}" in body
