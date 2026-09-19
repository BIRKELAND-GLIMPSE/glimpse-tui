"""BTC, MEMP, FEES and GP on the recorded sources, plus the Bitcoin arithmetic they lean on."""
import time

import pytest
from offline import fixture
from rich.cells import cell_len

from glimpse_tui.data import btcmath, feeds, quotes
from glimpse_tui.data.mempool import ProjectedTx
from glimpse_tui.funcs import btc, gp
from glimpse_tui.term import gobar
from glimpse_tui.term.hub import Hub


def text(lines) -> str:
    return "\n".join(ln.plain for ln in lines)


async def chain(hub: Hub) -> None:
    await feeds.poll_chain_once(hub)


# ── arithmetic ──────────────────────────────────────────────

def test_issuance_schedule():
    assert btcmath.subsidy_sats(0) == 50 * 10**8 and btcmath.subsidy_sats(209_999) == 50 * 10**8
    assert btcmath.subsidy_sats(210_000) == 25 * 10**8 and btcmath.subsidy_sats(840_000) == 312_500_000
    assert btcmath.subsidy_sats(64 * 210_000) == 0
    assert btcmath.issued_sats(0) == 50 * 10**8 and btcmath.issued_sats(209_999) == 10_500_000 * 10**8
    assert btcmath.issued_sats(10_000_000) / 1e8 == pytest.approx(btcmath.MAX_SUPPLY_BTC, abs=1e-3)
    h = btcmath.halving(967_731)
    assert h.era == 4 and h.next_height == 1_050_000 and h.blocks_left == 82_269 and h.subsidy_next == 156_250_000


def test_hashprice_and_fee_maths():
    usd, sats = btcmath.hashprice(312_500_000, 5_000_000, 144, 80_000, 9.5e20)
    assert sats == pytest.approx((312_500_000 + 5_000_000) * 144 / (9.5e20 / 1e15))
    assert usd == pytest.approx(sats / 1e8 * 80_000)
    assert btcmath.tx_vsize(1, 2) == 10.5 + 68 + 62 and btcmath.fee_sats(140.5, 4.2) == 591
    assert btcmath.kelly(0.6, 2.0) == pytest.approx(0.2) and btcmath.kelly(0.4, 2.0) < 0
    iv = btcmath.implied_vol(80_000, 79_000, 81_000, 3600)
    assert 0.5 < iv < 1.5                                                   # a 2.5% wide hourly band is about 90% annualised


# ── MEMP ────────────────────────────────────────────────────

async def test_memp_draws_the_train_the_picture_and_the_summary(no_network):
    hub = Hub()
    await chain(hub)
    pane = btc.MempPane(hub)
    await pane.reload()
    assert pane.error == ""
    out = text(pane.draw(90, 30))
    blocks = fixture("mempool/fees_mempool_blocks.json")
    more = round(sum(b["blockVSize"] for b in blocks[3:]) / 1e6)             # the last entry stands for every block behind it
    assert "NEXT BLOCKS" in out and "#1" in out and "#3" in out and f"+{more}" in out
    assert f"{blocks[0]['nTx']:,} tx" in out and "fee bands" in out         # no stream in tests: the picture is drawn from percentiles
    assert "#967,731" in out and "Foundry USA" in out and "1,965 blk" in out
    assert pane.provs and pane.provs[0].source == "mempool.space"
    assert all(cell_len(ln.plain) <= 46 for ln in pane.draw(46, 15))
    assert pane.export()[1][0][2] == blocks[0]["nTx"]


def test_the_block_picture_orders_by_fee_and_leaves_unbid_space_empty():
    txs = [ProjectedTx("a" * 64, 1000, 250_000, 0, 2.0), ProjectedTx("b" * 64, 9000, 250_000, 0, 120.0)]
    rows = btc.block_picture(txs, 20, 2)                                    # 80 pixels, 12.5 kvB each: half the block is bid for
    assert len(rows) == 2 and all(cell_len(r.plain) == 20 for r in rows)
    styles = [str(s.style) for r in rows for s in r.spans]
    from glimpse_tui import charts
    hot, cold = charts.snap(charts.fee_colour(120.0)), charts.snap(charts.fee_colour(2.0))
    assert hot in styles[0] and any(cold in s for s in styles)              # the expensive transaction is drawn first
    assert any("#0d0d0d" in s for s in styles)                              # and the unfilled half stays dark


async def test_memp_holds_the_heavy_subscription_only_while_open(no_network):
    hub = Hub()
    pane = btc.MempPane(hub)
    pane.on_mount()
    assert hub.stream_subscriptions() == [{"track-mempool-block": 0}] and hub.stream_commands.get_nowait() == {"track-mempool-block": 0}
    pane.on_unmount()
    assert hub.stream_subscriptions() == [] and hub.stream_commands.get_nowait() == {"track-mempool-block": -1}


def test_stream_messages_update_the_chain_and_a_new_block_flashes():
    hub = Hub()
    feeds.apply_ws(hub, fixture("mempool/ws_want_reply.json"))
    first = hub.chain.height
    assert first > 0 and hub.chain.mempool_blocks and hub.chain.fees and hub.chain.block_seen_at == 0
    feeds.apply_ws(hub, fixture("mempool/ws_projected_block_full.json"))
    assert len(hub.projected) == 6 and hub.projected_version == 1
    block = dict(fixture("mempool/blocks.json")[0], height=first + 1)
    feeds.apply_ws(hub, {"block": block})
    assert hub.chain.height == first + 1 and time.time() - hub.chain.block_seen_at < 2 and not hub.projected
    assert hub.events[-1]["kind"] == "block" and hub.events[-1]["height"] == first + 1


# ── FEES and BTC ────────────────────────────────────────────

async def test_fees_prices_a_transaction_and_the_calculator_keys_work(no_network):
    hub = Hub()
    await quotes.refresh(hub, ["BTC"])
    pane = btc.FeesPane(hub, None, ("2", "3", "p2tr"))
    await pane.reload()
    assert pane.error == "" and (pane.n_in, pane.n_out, pane.kind) == (2, 3, "p2tr")
    out = text(pane.draw(100, 30))
    precise = fixture("mempool/fees_precise.json")
    assert "next block" in out and btc.fee_text(precise["fastestFee"]) in out and "₿" in out and "$" in out
    assert "RECENT BLOCKS BY PERCENTILE" in out
    pane.key("]", "]")
    pane.key("y", "y")
    assert pane.n_in == 3 and pane.kind != "p2tr"
    assert all(cell_len(ln.plain) <= 44 for ln in pane.draw(44, 20))


async def test_btc_page_names_every_source(no_network):
    hub = Hub()
    await chain(hub)
    await quotes.refresh(hub, ["BTC"])
    pane = btc.BtcPane(hub)
    await pane.reload()
    out = text(pane.draw(110, 30))
    assert pane.error == "" and "chain oracle" in out and "81,337" in out   # Bitview's price, read from the chain alone
    assert "all-time high" in out and "halving" in out and "82,269 blocks" in out and "protocol maximum issued" in out
    assert "median of the venues that answered" in out and "coinbase" in out and "kraken" in out
    assert {p.source for p in pane.provs} >= {"bitstamp+coinbase+kraken", "bitview.space"}


# ── GP ──────────────────────────────────────────────────────

async def test_gp_charts_instruments_series_and_mixes(no_network):
    hub = Hub()
    book = hub.book
    one = gp.GpPane.from_command(hub, gobar.Command("GP", (book.get("BTC"),), ("1Y",)))
    await one.reload()
    assert one.error == "" and one.title == "BTC · 1Y" and "BTC" in text(one.draw(100, 20))
    many = gp.GpPane.from_command(hub, gobar.Command("GP", (book.get("BTC"), book.get("SPX"), book.get("EURUSD"))))
    await many.reload()
    out = text(many.draw(120, 24))
    assert "rebased to 100" in out and "SPX" in out and "EURUSD" in out
    assert {p.source.split(":")[0] for p in many.provs} >= {"fred"}
    series = gp.GpPane(hub, None, ("MVRV",))                                # an upper-case word that is no ticker is a series name
    await series.reload()
    assert series.error == "" and "mvrv" in series.title and "1.5" in text(series.draw(100, 20))
    mix = gp.GpPane.from_command(hub, gobar.Command("GP", (book.get("BTC"),), ("realized_price",)))
    await mix.reload()
    assert "on the left axis" in text(mix.draw(120, 24))
    assert all(cell_len(ln.plain) <= 50 for ln in many.draw(50, 12))
    assert one.export()[0] == ["time_utc", "BTC"] and len(one.export()[1]) > 100


async def test_gp_btc_hourly_continues_into_the_glimpse_forecast(no_network):
    hub = Hub()
    now = time.time()
    hub.forecast = lambda: [(now + 3600 * (i + 1) - now % 3600, 81_000 + 50 * i, 80_000 - 60 * i, 82_000 + 60 * i) for i in range(40)]
    pane = gp.GpPane.from_command(hub, gobar.Command("GP", (hub.book.get("BTC"),)))
    await pane.reload()
    out = text(pane.draw(110, 22))
    assert pane.view == "1H" and "NOW" in out and "░" in out and "Glimpse's median" in out
    pane.key("]", "]")
    assert pane.view == "1D" and pane.loaded_at == 0.0                      # a new window asks for a new load


async def test_a_series_that_is_not_in_the_catalogue_says_so(no_network):
    hub = Hub()
    hub.sources["bitview"].retries = 0
    pane = gp.GpPane(hub, None, ("definitely_not_a_series",))
    await pane.reload()
    assert pane.error and "bitview" in pane.error.lower()
