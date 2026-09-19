"""MINE, HASH, DIFF, HASHP, HALV, SUPL, LN and ORCL against the recorded sources. No screen, no network."""
import math

import pytest
from offline import fixture
from rich.cells import cell_len

from glimpse_tui.data import bitview, btcmath, core, quotes
from glimpse_tui.funcs import mining
from glimpse_tui.term import registry
from glimpse_tui.term.hub import Hub

TIP = 967_731
PANES = [(mining.MinePane, ()), (mining.MinePane, ("foundryusa",)), (mining.HashPane, ("3m",)), (mining.DiffPane, ()), (mining.HashpPane, ()),
         (mining.HalvPane, ()), (mining.SuplPane, ()), (mining.LnPane, ()), (mining.OrclPane, ())]


async def loaded(cls, args=(), hub=None):
    hub = hub or Hub()
    for s in hub.sources.values():
        s.bucket = core.TokenBucket(1000, 1000)                              # the recorded internet needs no manners
    pane = cls(hub, None, args)
    await pane.reload()
    assert pane.error == ""
    return pane


def text(pane, w=100, h=40) -> str:
    return "\n".join(ln.plain for ln in pane.draw(w, h))


# ── the maths ───────────────────────────────────────────────

def test_shares_are_computed_from_block_counts_and_sum_to_one():
    data = fixture("mempool/mining_pools_1w.json")
    assert all("share" not in p for p in data["pools"])                      # the source sends none (SOURCES.md)
    shares = mining.pool_shares(data["pools"])
    assert sum(shares) == pytest.approx(1.0) and shares[0] == pytest.approx(257 / 1025)
    assert mining.pool_shares([]) == [] and mining.pool_shares([{"blockCount": 0}]) == [0.0]
    assert mining.network_hashrate(data, "24h") == float(data["lastEstimatedHashrate"])
    assert mining.network_hashrate(data, "3d") == float(data["lastEstimatedHashrate3d"])
    assert mining.network_hashrate(data, "1y") == float(data["lastEstimatedHashrate1w"])
    assert mining.network_hashrate({"lastEstimatedHashrate": 5.0}, "1y") == 5.0   # a backend that sends only the one estimate


def test_hashprice_matches_a_hand_calculation_from_the_fixtures():
    rewards, fees = fixture("mempool/mining_blocks_rewards_1w.json"), fixture("mempool/mining_blocks_fees_1w.json")
    hashrate = fixture("mempool/mining_hashrate_3m.json")["currentHashrate"]
    price = fixture("mempool/prices.json")["USD"]
    bpd = mining.blocks_per_day(rewards)
    assert bpd == pytest.approx((967_731 - 967_726) / (1_789_842_486 - 1_789_839_014) * 86_400)     # 5 blocks in 3,472 s: 124.4 a day
    mean_fees = (2_912_039 + 2_997_793 + 1_820_988 + 1_824_332 + 5_029_947) / 5
    assert mining.mean([f["avgFees"] for f in fees]) == mean_fees
    sats = (312_500_000 + mean_fees) * bpd / (hashrate / 1e15)
    usd, got = btcmath.hashprice(btcmath.subsidy_sats(TIP), mean_fees, bpd, price, hashrate)
    assert got == pytest.approx(sats) and usd == pytest.approx(sats / 1e8 * price)
    assert round(got) == 42_050 and round(usd, 2) == 34.25
    assert mining.blocks_per_day(rewards[:1]) is None and mining.blocks_per_day([]) is None
    assert btcmath.hashprice(1, 1, 144, 1, 0) == (0.0, 0.0)


def test_halving_at_the_edges_of_an_era():
    assert btcmath.halving(0).era == 0 and btcmath.halving(0).blocks_left == 210_000 and btcmath.halving(0).subsidy_now == 50 * 10**8
    last = btcmath.halving(209_999)
    assert (last.era, last.blocks_left, last.next_height, last.subsidy_next) == (0, 1, 210_000, 25 * 10**8)
    first = btcmath.halving(210_000)
    assert (first.era, first.blocks_left, first.subsidy_now) == (1, 210_000, 25 * 10**8)
    assert btcmath.halving(840_000).era == 4 and btcmath.halving(840_000).subsidy_now == 312_500_000
    tip = btcmath.halving(TIP, 829.039)
    assert (tip.era, tip.next_height, tip.blocks_left, tip.subsidy_next) == (4, 1_050_000, 82_269, 156_250_000)
    assert tip.est_seconds == pytest.approx(82_269 * 829.039)


def test_issued_supply_at_known_heights():
    assert btcmath.issued_sats(0) == 50 * 10**8
    assert btcmath.issued_sats(209_999) == 10_500_000 * 10**8
    assert btcmath.issued_sats(210_000) == 10_500_025 * 10**8
    assert btcmath.issued_sats(TIP) / 1e8 == 19_687_500 + (TIP + 1 - 840_000) * 3.125 == 20_086_662.5
    assert btcmath.issued_sats(10_000_000) / 1e8 == pytest.approx(btcmath.MAX_SUPPLY_BTC, abs=1e-4)
    assert btcmath.annual_inflation(TIP) == pytest.approx(3.125 * 144 * 365.25 / 20_086_662.5)


def test_the_issuance_schedule_ends_at_the_cap():
    rows = mining.era_rows(TIP, 1_789_842_486.0)
    assert len(rows) == 33 and [r["known"] for r in rows[:6]] == [True] * 5 + [False]
    assert rows[0]["supply"] == 10_500_000 and rows[4]["subsidy"] == 312_500_000 and rows[-1]["subsidy"] == 1
    assert rows[-1]["supply"] == pytest.approx(btcmath.MAX_SUPPLY_BTC, abs=1e-4)
    assert btcmath.subsidy_sats(rows[-1]["start"] + 210_000) == 0
    assert all(a["at"] < b["at"] for a, b in zip(rows, rows[1:], strict=False))


def test_oracle_bins_and_the_folded_histogram():
    assert bitview.oracle_bin(100, 81_337) == 1018 and bitview.oracle_bin(10, 81_337) == 818 and bitview.oracle_bin(1_000, 81_337) == 1218
    hist = fixture("bitview/oracle_histogram_payments_live.json")
    cols, lo, per = mining.oracle_columns(hist, 81_337.07, 110)
    assert lo == 618 and per == 8 and len(cols) == math.ceil(801 / 8) <= 110
    assert cols[(1018 - lo) // per] == hist[1018] == 129 == max(cols)        # the $100 spike survives the fold, under its marker
    assert mining.spike_ratio(hist, 1018) > 3 and mining.spike_ratio(hist, -5) is None
    assert mining.oracle_columns(hist, 0.0, 80) == ([], 0, 1)


def test_small_helpers():
    assert mining.change_over([100.0] * 7 + [110.0] * 7, 7) == pytest.approx(0.10) and mining.change_over([1.0] * 5, 7) is None
    assert mining.trailing_mean([0, 1, 2, 3], [4.0, 8.0, 0.0, 4.0], 2) == [4.0, 6.0, 4.0, 2.0]
    assert mining.dur(829) == "13m 49s" and mining.dur(3 * 3600 + 60) == "3h 01m" and mining.dur(14 * 86400 + 7200) == "14d 2h"
    assert mining.clean("ACINQ ⚡ 闪电") == "ACINQ" and mining.clean("") == "?"
    xs, ys = mining.difficulty_steps([{"time": 10, "difficulty": 110.0, "adjustment": 1.1}], 0, 20)
    assert (xs, ys) == ([0, 10.0, 10.0, 20], [pytest.approx(100.0), pytest.approx(100.0), 110.0, 110.0])
    assert mining.ln_as_of({"added": "2026-08-30T00:00:00.000Z"}) == 1_788_048_000 and mining.ln_as_of({}) == 0.0


def test_grid_drops_columns_in_order_and_never_overflows():
    specs = [mining.C("pool", "left", shrink=6), mining.C("blocks"), mining.C("", "left", drop=1), mining.C("health", drop=2)]
    rows = [["Foundry USA", "257", mining.Bar(1.0), "98.80%"], ["AntPool", "203", mining.Bar(0.5), "98.40%"]]
    wide, narrow = mining.grid(specs, rows, 60), mining.grid(specs, rows, 20)
    assert "health" in wide[0].plain and "█" in wide[2].plain and "Foundry USA" in wide[2].plain
    assert "health" not in narrow[0].plain and "257" in narrow[2].plain
    assert all(cell_len(ln.plain) <= 60 for ln in wide) and all(cell_len(ln.plain) <= 20 for ln in narrow)


# ── the pages ───────────────────────────────────────────────

async def test_mine_lists_pools_by_share_and_opens_one(no_network):
    pane = await loaded(mining.MinePane)
    page = text(pane)
    assert pane.title == "pools · 1w" and pane.provs[0].source == "mempool.space"
    assert "1,025 blocks" in page and "17 pools" in page and "930.2 EH/s" in page
    first = next(ln for ln in page.split("\n") if "Foundry USA" in ln)
    assert "257" in first and "25.1%" in first and "█" in first and "98.80%" in first and "−2.53%" in first
    assert f"{257 / 1025 * 930.166759114616:,.1f}" in first                 # share of the week's network estimate: 233.2 EH/s
    assert pane.n_rows == 17 and pane.enter() == "MINE foundryusa"
    pane.cur = 1
    assert pane.enter() == "MINE antpool"
    header, rows = pane.export()
    assert header[:5] == ["rank", "pool", "slug", "blocks", "share"] and len(rows) == 17
    assert sum(r[4] for r in rows) == pytest.approx(1.0, abs=1e-4)
    assert pane.key("]", "]") and pane.period == "1m" and pane.command() == "MINE 1m" and pane.loaded_at == 0.0
    assert mining.MinePane(Hub(), None, ("3Y",)).period == "3y"
    assert no_network.missing == []


async def test_mine_one_pool_shows_its_windows_and_recent_blocks(no_network):
    pane = await loaded(mining.MinePane, ("foundryusa",))
    page = text(pane, 120, 40)
    assert pane.title == "pool · Foundry USA" and "189.7 EH/s" in page and "99.4%" in page
    assert "74,459" in page and "25.07%" in page and "20.57%" in page and "7.69%" in page
    block = next(ln for ln in page.split("\n") if ln.startswith("967,731"))
    assert "4,386" in block and "₿5,029,947" in block and "₿317,529,947" in block and "99.68%" in block
    assert pane.n_rows == 2 and pane.enter() == "BLK 967731" and pane.command() == "MINE foundryusa"
    assert not pane.key("]", "]")                                           # a pool has no window to step
    assert pane.export()[1][0][:3] == [967731, 1789842486, 4386]
    assert no_network.missing == []


async def test_hash_draws_hashrate_and_difficulty_on_one_chart(no_network):
    pane = await loaded(mining.HashPane, ("3m",))
    page = text(pane)
    assert pane.title.endswith("3m") and [p.source for p in pane.provs] == ["mempool.space", "bitview.space"]
    assert "933.3 EH/s" in page and "132.76 T" in page and "3m high" in page and "7d" in page and "30d" in page
    assert "hashrate, EH/s, 7 day mean" in page and "difficulty, T  (left axis)" in page and any(0x2800 < ord(ch) <= 0x28FF for ch in page)
    assert "bitview hash_rate" in page and "daily · 18 Sep 2026" in page
    assert pane.key("L", "L") and pane.log_y and "log scale" in text(pane)
    assert pane.key("[", "[") and pane.period == "3m" and pane.key("]", "]") and pane.period == "6m" and pane.command() == "HASH 6m"
    header, rows = pane.export()
    assert header == ["timestamp", "day_utc", "avg_hashrate_hs", "difficulty"] and len(rows) == 92
    assert rows[-1][3] == 127450789715843.1 and rows[0][3] is not None       # the last daily point is from before that day's retarget
    assert mining.HashPane(Hub(), None, ()).period == "1y"
    assert no_network.missing == []


async def test_hash_default_year_loads_from_its_fixture(no_network):
    pane = await loaded(mining.HashPane)
    assert "1y high" in text(pane) and len(pane.data["hashrates"]) == 365 and no_network.missing == []


async def test_diff_shows_the_epoch_and_a_year_of_retargets(no_network):
    pane = await loaded(mining.DiffPane)
    page = text(pane, 110, 40)
    assert "51 of 2,016" in page and "1,965" in page and "#969,696" in page and "2.53%" in page
    assert "13m 49s" in page                                                # timeAvg is milliseconds at the source
    assert "04 Oct 07:54 UTC" in page                                       # so is estimatedRetargetDate
    assert "19 Sep 07:09 UTC" in page                                       # previousTime is seconds
    assert "−5.98%" in page and "+4.16%" in page and "19 blocks behind schedule" in page and "124.82 T" in page
    first = next(ln for ln in page.split("\n") if ln.startswith("19 Sep 2026"))
    assert "967,680" in first and "132.76 T" in first and "+4.16%" in first
    assert "−10.09%" in page and "green up, red down" in page and "UNVERIFIED" not in page
    header, rows = pane.export()
    assert header == ["time", "day_utc", "height", "difficulty", "change"] and rows[0][2] == 967680 and rows[0][4] == pytest.approx(0.04163)
    assert all(p.source == "mempool.space" for p in pane.provs) and no_network.missing == []


async def test_diff_never_shows_bitviews_estimate_as_the_number(no_network):
    hub = Hub()
    no_network.fail.add("mempool.space")
    for s in hub.sources.values():
        s.retries = 0
    pane = await loaded(mining.DiffPane, hub=hub)
    page = text(pane, 110, 40)
    assert pane.unsafe == "bitview.space" and "UNVERIFIED" in page and "−24.89% unverified" in page
    assert "1,965" in page and "124.82 T" not in page                       # the block counts stand; no next difficulty is derived from it


async def test_hashp_computes_hashprice_from_observed_blocks(no_network):
    pane = await loaded(mining.HashpPane)
    x = pane.inputs()
    assert x["height"] == TIP and x["subsidy"] == 312_500_000 and x["price"] == 81_445 and x["price_label"] == "mempool.space"
    assert x["bpd"] == pytest.approx(124.42, abs=0.01) and round(x["sats"]) == 42_050 and round(x["usd"], 2) == 34.25
    page = text(pane, 110, 60)
    assert "$34.25" in page and "₿42,050" in page and "per PH/s a day" in page and "$0.0342 per TH/s" in page
    assert "₿312,500,000" in page and "₿2,917,020" in page and "0.92%" in page and "124.4" in page and "933.3 EH/s" in page
    assert "452.71 BTC" in page and "2.709 BTC" in page and "0.60%" in page and "688,065" in page       # reward-stats totals are strings
    assert "fee share %" in page and "1.58" in page and "revenue a day, USD" in page
    assert pane.export()[1][-1] == [1789842486, 967731, 317529947.0, 5029947.0, round(5029947 / 317529947, 6), 81441.0]
    assert no_network.missing == []


async def test_hashp_prefers_the_composite_quote(no_network):
    hub = Hub()
    await quotes.refresh(hub, ["BTC"])
    pane = await loaded(mining.HashpPane, hub=hub)
    x = pane.inputs()
    assert x["price"] == hub.quotes["BTC"].price and x["price_label"] == "composite" and "composite" in text(pane)
    assert hub.quotes["BTC"].prov in pane.provs


async def test_halv_counts_down_and_lists_the_schedule(no_network):
    pane = await loaded(mining.HalvPane)
    page = text(pane, 110, 60)
    assert "82,269 blocks" in page and "#1,050,000" in page and "~789 days" in page          # 82,269 blocks at 829 s
    assert "13m 49s a block" in page and "~571 days" in page                                  # and at the ten minute target
    assert "3.125 BTC" in page and "1.5625 BTC" in page and "60.82%" in page and "era 5 of 33" in page
    era1 = next(ln for ln in page.split("\n") if ln.lstrip().startswith("1 "))
    assert "50" in era1 and "03 Jan 2009" in era1 and "10,500,000.0000" in era1 and "50.000000%" in era1
    era6 = next(ln for ln in page.split("\n") if "1,050,000 " in ln and "~" in ln)
    assert "1.5625" in era6 and "20,671,875.0000" in era6
    assert "20,999,999.9769" in page and len(pane.export()[1]) == 33
    hub = Hub()
    hub.chain.height, hub.chain.difficulty = 840_000, {}
    assert "210,000 blocks" in text(mining.HalvPane(hub, None, ())) and "no pace observed" in text(mining.HalvPane(hub, None, ()))
    assert no_network.missing == []


async def test_supl_labels_the_protocol_maximum_and_every_daily_value(no_network):
    pane = await loaded(mining.SuplPane)
    page = text(pane, 110, 40)
    assert "20,086,662.50 BTC" in page and "protocol maximum issued" in page and "#967,731" in page and "95.651%" in page
    assert "913,337.48 BTC" in page
    supply = next(ln for ln in page.split("\n") if "circulating_supply" in ln)
    assert "20,086,432.38 BTC" in supply and "daily · 19 Sep 2026" in supply
    assert "−230.12" in page                                                 # bitview's figure against the protocol maximum
    assert "325.7 BTC" in page and "450.0 BTC" in page and "225.0 BTC" in page                # 3.125 x 86,400 / 829.039; x 144; halved
    assert f"{btcmath.annual_inflation(TIP):.3%}" in page and f"{btcmath.annual_inflation(TIP, 86_400 / 829.039):.3%}" in page
    assert "475.00 BTC" in page and "18 Sep 2026" in page                                    # issued on the last complete day
    utxo = next(ln for ln in page.split("\n") if "unspent outputs" in ln)
    assert "165,266,768" in utxo and "daily · 18 Sep 2026" in utxo and "−0.57%" in utxo
    assert any(ch in page for ch in "▁▂▃▄▅▆▇█") and len(pane.export()[1]) == 60
    assert no_network.missing == []


async def test_supl_still_shows_the_computed_half_without_bitview(no_network):
    hub = Hub()
    no_network.fail.add("bitview.space")
    for s in hub.sources.values():
        s.retries = 0
    page = text(await loaded(mining.SuplPane, hub=hub))
    assert "20,086,662.50 BTC" in page and "bitview" not in page


async def test_ln_says_when_its_statistics_are_stale(no_network):
    pane = await loaded(mining.LnPane)
    page = text(pane, 110, 60)
    top = page.split("\n")[0]
    assert "STALE" in top and "as of 30 Aug 2026" in top and "days old" in top
    assert pane.provs[0].stale() and pane.provs[0].as_of == 1_788_048_000 and pane.provs[0].delay == "daily"
    assert "3,753.80 BTC" in page and "($305.7M)" in page and "32,518" in page and "16,230" in page
    assert "tor 7,980" in page and "clearnet 4,599" in page and "₿11,543,750" in page and "823 ppm" in page and "0.904 sat" in page
    assert "capacity, BTC" in page and "channels  (left axis)" in page
    assert "United States" in page and "4,442" in page and "30.5%" in page
    assert page.index("Amazon.com") < page.index("DataWeb") < page.index("DigitalOcean")     # by capacity, not the order received
    assert "bfx-lnd0" in page and "425.2" in page and "ACINQ" in page and "1,836" in page
    assert len(pane.export()[1]) == len(fixture("mempool/lightning_statistics_3m.json"))
    assert no_network.missing == []


async def test_orcl_sets_the_oracle_beside_the_composite_and_marks_the_round_dollars(no_network):
    hub = Hub()
    await quotes.refresh(hub, ["BTC"])
    pane = await loaded(mining.OrclPane, hub=hub)
    composite = hub.quotes["BTC"].price
    lines = [ln.plain for ln in pane.draw(110, 40)]
    page = "\n".join(lines)
    assert "81,337.07" in page and f"{composite:,.2f}" in page and "exchange composite" in page
    assert f"{abs(81_337.07 / composite - 1):.2%}" in page and f"{abs(81_337.07 - composite):,.2f} USD" in page
    assert "no exchange is involved" in page and "PAYMENTS HISTOGRAM" in page
    carets = next(i for i, ln in enumerate(lines) if "▲" in ln)
    cols, lo, per = mining.oracle_columns(pane.hists["payments"], pane.oracle, 110)
    marked = [(bitview.oracle_bin(u, pane.oracle) - lo) // per for u in mining.ROUND_DOLLARS]
    assert [i for i, ch in enumerate(lines[carets]) if ch == "▲"] == marked
    spike = (1018 - lo) // per
    assert lines[carets][spike] == "▲" and lines[carets - 12][spike] != " "        # the $100 column reaches the top row of the chart
    assert "$100" in lines[carets + 1] and "$10" in lines[carets + 1] and "$1k" in lines[carets + 1]
    hundred = next(ln for ln in lines if ln.lstrip().startswith("$100 "))
    assert "1018" in hundred and "₿122,945" in hundred and "129" in hundred
    assert pane.key("o", "o") and pane.kind == "outputs" and "OUTPUTS HISTOGRAM" in text(pane) and pane.command() == "ORCL outputs"
    header, rows = pane.export()
    assert header == ["bin", "sats", "usd_at_oracle", "payments", "outputs"] and [r for r in rows if r[0] == 1018][0][3:] == [129, 292]
    assert no_network.missing == []


async def test_orcl_names_the_stand_in_price_when_no_composite_has_arrived(no_network):
    page = text(await loaded(mining.OrclPane))
    assert "81,445.00" in page and "mempool.space, no composite yet" in page


# ── every page, every size ──────────────────────────────────

@pytest.mark.parametrize(("cls", "args"), PANES)
async def test_no_line_is_wider_than_the_pane_and_every_glyph_is_one_cell(cls, args, no_network):
    pane = await loaded(cls, args)
    assert pane.title and pane.provs
    for w, h in ((44, 10), (44, 40), (70, 24), (100, 40), (196, 60)):
        lines = pane.draw(w, h)
        assert lines, (cls.code, w, h)
        assert max(cell_len(ln.plain) for ln in lines) <= w, (cls.code, w, h)
        assert not [ch for ln in lines for ch in ln.plain if cell_len(ch) != 1], (cls.code, w, h)
        assert "—" not in "".join(ln.plain for ln in lines)
    assert isinstance(pane.hint(), str) and pane.menu() and pane.export()
    assert no_network.missing == []


def test_the_family_registers_with_help_for_every_function():
    registry.load_all()
    assert "mining" not in registry.failed()
    for code in ("MINE", "HASH", "DIFF", "HASHP", "HALV", "SUPL", "LN", "ORCL"):
        fn = registry.get(code)
        assert fn and fn.category == "Bitcoin" and fn.make and len(fn.help) > 400 and "—" not in fn.help + fn.summary, code
