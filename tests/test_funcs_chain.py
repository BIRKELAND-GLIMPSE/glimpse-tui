"""BLK, TX, ADDR and RBF on the recorded chain, the Esplora fallback, the Electrum transcript and Core RPC. No network."""
import base64
import json
import ssl
import time

import httpx
import pytest
from offline import ROOT, fixture
from rich.cells import cell_len

from glimpse_tui.data import core, core_rpc, electrum, esplora, http
from glimpse_tui.funcs import chain
from glimpse_tui.term import config, registry
from glimpse_tui.term.hub import Hub, Quote

BLOCK = "00000000000000000000d7decd57c4221866464aef04799a82be9d4d032d056d"          # height 967,730, SpiderPool
TX = "28efab4691b845fc17af88654a387de8ef6798227b6a654721788f4a9838381f"             # confirmed, 1 in, 2 out, both spent
TX_DATA = "025cc8e573dc2b1ea5db8b66c8257a058e6eb8af82d83e0bdf8329e89909cd07"        # confirmed, carries an OP_RETURN
TX_POOL = "d31456f50785aea9ad8eef987b654f82a3fc50754d8cc05b66c3fb520c5d1365"        # unconfirmed replacement with a parent
REPLACED = "b7162c186f01cde58ceb45cd7d340d386fe10346cd80ebea1c86260f8819fb70"
ADDRESS = "12cbQLTFMXRnSzktFkuoG3eHoMeFtpTu3S"
WIDTHS = (44, 52, 64, 80, 100, 132, 196)


def make_hub(price: float | None = 81_420.0) -> Hub:
    hub = Hub()
    for s in hub.sources.values():
        s.retries = 0                                                               # a missing fixture fails at once
        s.bucket = core.TokenBucket(1000, 1000)                                     # the recorded internet needs no manners
    if price:
        hub.quotes["BTC"] = Quote("BTC", price, core.Provenance("test", time.time(), time.time(), "live"))
    return hub


async def opened(cls, hub, *args):
    pane = cls(hub, None, args)
    await pane.reload()
    assert pane.error == "", pane.error
    return pane


def page(pane, w: int = 100, h: int = 40) -> str:
    return "\n".join(ln.plain for ln in pane.draw(w, h))


async def every_pane(hub):
    return [await opened(chain.BlkPane, hub), await opened(chain.BlkPane, hub, "967730"), await opened(chain.TxPane, hub, TX),
            await opened(chain.TxPane, hub, TX_DATA), await opened(chain.TxPane, hub, TX_POOL), await opened(chain.AddrPane, hub, ADDRESS),
            await opened(chain.RbfPane, hub)]


# ── BLK ─────────────────────────────────────────────────────

async def test_blk_lists_recent_blocks_and_pages_to_older_ones(no_network):
    hub = make_hub()
    pane = await opened(chain.BlkPane, hub)
    text = page(pane, 120)
    assert pane.title == "recent blocks" and pane.provs[0].source == "mempool.space"
    for want in ("967,731", "Foundry USA", "4,386", "99.68%", "967,730", "SpiderPool", "3,967", "100%", "3.143", "0.018", "1.61", "3.99",
                 "MARA Pool"):
        assert want in text, want
    assert pane.n_rows == 3 and pane.enter() == f"BLK {fixture('mempool/blocks.json')[0]['id']}"
    pane.cur = 1
    assert pane.enter() == f"BLK {BLOCK}"
    pane.cur = 2
    assert pane.on_key_("j", "j") and pane.want_older and pane.loaded_at == 0      # moving past the last row asks for older blocks
    await pane.reload()
    assert "https://mempool.space/api/v1/blocks/967728" in no_network.seen and not no_network.missing
    assert pane.n_rows == 3 or len(pane.blocks) == 6
    text = page(pane, 120)
    assert "967,728" in text and "967,726" in text and pane.blocks[967728] == fixture("mempool/blocks_from_967728.json")[0]
    assert pane.enter() == f"BLK {pane.blocks[967728]['id']}"                       # the cursor landed on the first older block
    header, rows = pane.export()
    assert header[0] == "height" and [r[0] for r in rows] == [967731, 967730, 967729, 967728, 967727, 967726]
    assert pane.key("n", "n") and pane.want_older and pane.hint() and pane.menu()


async def test_blk_shows_one_block_by_height(no_network):
    hub = make_hub()
    pane = await opened(chain.BlkPane, hub, "967,730")
    text = page(pane, 110)
    assert pane.title == "block 967,730" and not no_network.missing
    assert f"https://mempool.space/api/v1/block/{BLOCK}" in no_network.seen
    for want in (BLOCK, "SpiderPool", "3,967 tx", "19 Sep 2026 17:59:44 UTC", "2 confirmations", "0x3fff0000", "0x17021ec5", "2,026,018,995",
                 "132.76 T", "9ce4d17e09635b34b7e2bf83b2574d6f1c0b7be59c30895d14a2d3dc867cfa5b", "jSpiderPool/1558/",
                 "median", "0.52", "0.55", "0.78", "3.5", "60", "₿312,500,000", "₿1,824,332", "₿314,324,332", "0.58%", "$255,923",
                 "1-4 of 3,967", "₿11,340", "coinbase", "₿1,885,111"):
        assert want in text, want
    assert pane.enter() == "TX 8fbafa3bb14f6f5e402a3ae2880f5a4a9cdea7bb0d1f54d3d669dd52b2145485"
    pane.cur = 1
    assert pane.enter() == f"TX {TX}"
    assert not pane.provs[0].stale(time.time() + 50)                                # a mined block is not a stale value
    header, rows = pane.export()
    assert header[1] == "txid" and rows[1][1] == TX and rows[1][5] == 11340 and round(rows[1][6], 2) == 60.16
    assert pane.menu()[:2] == [("previous", "BLK 967729"), ("next", "BLK 967731")]
    assert pane.key("]", "]") and pane.page == 1 and pane.loaded_at == 0 and not pane.key("x", "x")
    assert "26-50 of 3,967" in page(pane) and "loading this page" in page(pane)
    assert pane.key("[", "[") and pane.page == 0 and not pane.key("[", "[")


def test_the_coinbase_tag_keeps_only_readable_runs():
    sig = fixture("mempool/block.json")["extras"]["coinbaseSignatureAscii"]
    assert chain.coinbase_tag(sig) == "jSpiderPool/1558/" and chain.coinbase_tag("") == ""
    assert chain.coinbase_tag("\x03ab\x00/Foundry USA Pool/") == "/Foundry USA Pool/"


# ── TX ──────────────────────────────────────────────────────

async def test_tx_explains_a_confirmed_transaction(no_network):
    hub = make_hub()
    pane = await opened(chain.TxPane, hub, TX)
    text = page(pane, 140)
    assert not no_network.missing and pane.title.startswith("tx 28efab46")
    assert "1 input, 2 outputs, pays 60.2 sat/vB, confirmed 1 block ago, does not signal RBF." in text
    for want in (TX, "confirmed", "967,730", "confirmations 2", "₿11,340 ($9.23)", "60.2 sat/vB", "188.50 vB", "754 WU", "379", "locktime 0",
                 "no signal",
                 "bc1qwqdg6squsna38e46795at95yu9atm8azzmyvckulcc7kytlcckxswvvzej", "p2wsh", "p2wpkh", "₿25,854,516", "₿2,500,000", "$2,035.50",
                 "₿23,343,176", "spent by 90b19c…5f161b", "00d2c6…c348c8:1"):
        assert want in text, want
    assert "CPFP" not in text and "REPLACEMENTS" not in text                        # neither exists for this transaction
    assert pane.n_rows == 3
    assert pane.enter() == "TX 00d2c6a592106c4d4717fc093fbf2af4d6b938aaa151ad072937d26f1cc348c8"
    pane.cur = 1
    assert pane.enter() == "ADDR bc1qxdn2rz8a76kcdqz47yg788tzh06nl7ujc2k4v6"
    hub.chain.height = 967_743                                                      # the stream's tip wins over the polled one
    assert "confirmed 13 blocks ago" in page(pane) and "confirmations 14" in page(pane)
    header, rows = pane.export()
    assert header[0] == "side" and [r[0] for r in rows] == ["input", "output", "output"] and rows[1][4] == 2_500_000
    assert pane.menu()[0] == ("block", "BLK 967730")


async def test_tx_decodes_an_op_return_as_text(no_network):
    pane = await opened(chain.TxPane, make_hub(), TX_DATA)
    text = page(pane, 100)
    assert "to:USDT(TRON):TNszJgNMfqyoqVup3oYyJHVWEvWck7bYkS" in text and "one OP_RETURN" in text and "OP_RETURN · output 1" in text
    assert "data, unspendable" in text and "unspent" in text and "6.21 sat/vB" in text and "₿1,000" in text and "p2tr" in text
    pane.cur = 2
    assert pane.enter() is None                                                     # an OP_RETURN has no address to open
    assert chain.op_return_text("6a04deadbeef") == ("deadbeef", False)             # not UTF-8: hex
    assert chain.op_return_text("6a4c0568656c6c6f") == ("hello", True)             # OP_PUSHDATA1
    assert chain.op_return_text("6a026869" + "0121") == ("hi!", True)              # two pushes, joined
    assert chain.op_return_text("6a03e4bda0")[1] is False                           # a double-width glyph stays hex


async def test_tx_in_the_mempool_shows_first_seen_package_and_replacements(no_network):
    hub = make_hub()
    pane = await opened(chain.TxPane, hub, TX_POOL)
    text = page(pane, 120)
    assert f"https://mempool.space/api/v1/transaction-times?txId[]={TX_POOL}" in no_network.seen
    assert all(u.endswith("/outspends") for u in no_network.missing)                # mempool.space itself answered 404 for it on the day
    assert "1 input, 1 output, pays 20.5 sat/vB, waiting in the mempool, signals RBF, replaced an earlier version." in text
    for want in ("in the mempool", "first seen 19 Sep 18:54 UTC", "block 1 of the queue", "₿2,245", "effective 3.21 sat/vB",
                 "signals replaceability", "0xfffffffd", "CPFP PACKAGE", "ancestor", "₿11,086", "4,041", "REPLACEMENTS", "1 replaced",
                 "b7162c18…8819fb70", "3.01", "+0.20", "+6.6%",
                 "+₿830", "after 63s", "latest"):
        assert want in text, want
    assert pane.n_rows == 1 + 1 + 1 + 2
    pane.cur = 2
    assert pane.enter() == "TX ab3cf8211d5d91f9a17771007fd7e5a92f6b3f557ff094dfd6d76e55b6736928"       # the unconfirmed parent
    pane.cur = 4
    assert pane.enter() == f"TX {REPLACED}"
    assert pane.menu()[0] == ("MEMP", "MEMP")


async def test_tx_rejects_what_is_not_a_txid(no_network):
    pane = chain.TxPane(make_hub(), None, ("nonsense",))
    await pane.reload()
    assert "64-character" in pane.error and not no_network.seen and pane.draw(80, 20) == []


def test_replacement_chains_stay_in_one_column_and_branches_indent():
    deep = fixture("mempool/replacements.json")[1]
    flat = chain.walk_tree(deep)
    assert len(flat) == 27 and {lead for lead, _, _ in flat[1:]} == {"└─ "}
    fork = {"tx": {"txid": "c"}, "replaces": [{"tx": {"txid": "a"}, "replaces": [{"tx": {"txid": "z"}, "replaces": []}]},
                                              {"tx": {"txid": "b"}, "replaces": []}]}
    assert [lead for lead, _, _ in chain.walk_tree(fork)] == ["", "├─ ", "│  └─ ", "└─ "]


# ── ADDR ────────────────────────────────────────────────────

async def test_addr_shows_balance_utxos_and_history(no_network):
    hub = make_hub()
    pane = await opened(chain.AddrPane, hub, ADDRESS)
    text = page(pane, 120)
    assert not no_network.missing and pane.provs[0].source == "mempool.space"
    for want in (ADDRESS, "₿44,405,441 ($36,155)", "0.44405441", "129 tx", "funded outputs 132", "unspent 132", "last seen 14 Sep 2026",
                 "oldest loaded 19 May 2026", "₿27,323", "410,787", "08 May 2016",
                 "f5b204652306e30faa99600994482f6fc9df8ba12df3576d0833091eb8270e64:3",
                 "+₿1,390", "+₿828", "+₿546", "966,915", "3 of 129 loaded"):
        assert want in text, want
    assert pane.n_rows == 5 + 3
    assert pane.enter() == "TX f5b204652306e30faa99600994482f6fc9df8ba12df3576d0833091eb8270e64"       # the largest UTXO
    pane.cur = 5
    assert pane.enter() == "TX e6028e3e834887d04f279c923649df519464bf4e36dcfa495a3933767b40161a"       # the newest transaction
    assert pane.key("]", "]") and pane.loaded_at == 0
    await pane.reload()
    assert len(pane.history) == 6 and pane.cur == 8 and not no_network.missing     # three older ones, cursor on the first of them
    assert "6 of 129 loaded" in page(pane, 120)
    header, rows = pane.export()
    assert header[0] == "kind" and [r[0] for r in rows].count("utxo") == 5 and rows[5][5] == 1390


async def test_addr_survives_the_too_many_utxos_answer(no_network, monkeypatch):
    hub = make_hub()
    body = (ROOT / "mempool" / "address_utxo_too_many_400.txt").read_text()
    assert body.startswith("Too many unspent transaction outputs")

    async def refuse(a):
        raise core.SourceError("mempool.space: HTTP 400")                           # what Source.get makes of that plain-text 400
    for key in hub.bitcoin_order():
        monkeypatch.setattr(hub.sources[key], "address_utxo", refuse)
    pane = await opened(chain.AddrPane, hub, ADDRESS)
    text = page(pane, 100)
    assert "500 or fewer" in text and "₿44,405,441" in text and "+₿1,390" in text and pane.n_rows == 3
    assert pane.enter() == "TX e6028e3e834887d04f279c923649df519464bf4e36dcfa495a3933767b40161a"


async def test_addr_watch_explains_first_and_saves_on_the_second_w(no_network):
    hub = make_hub()
    pane = await opened(chain.AddrPane, hub, ADDRESS)
    assert pane.key("w", "w") and pane.asking and hub.cfg["wallet"]["watch"] == []
    text = page(pane, 90)
    assert "WATCH THIS ADDRESS?" in text and "wallet.watch" in text and "public backend" in text and "holds no keys" in text
    assert pane.key("x", "x") and not pane.asking and hub.cfg["wallet"]["watch"] == []         # any other key cancels
    pane.key("w", "w")
    pane.key("w", "w")
    assert hub.cfg["wallet"]["watch"] == [ADDRESS] and config.load()["wallet"]["watch"] == [ADDRESS]
    assert "Watching" in page(pane, 90)
    pane.key("w", "w")
    assert "Already watched" in page(pane, 90) and hub.cfg["wallet"]["watch"] == [ADDRESS]


def test_net_effect_counts_both_sides():
    tx = {"vin": [{"prevout": {"scriptpubkey_address": "a", "value": 900}}, {"prevout": {"scriptpubkey_address": "b", "value": 50}}],
          "vout": [{"scriptpubkey_address": "a", "value": 300}, {"scriptpubkey_address": "c", "value": 600}]}
    assert chain.net_effect(tx, "a") == -600 and chain.net_effect(tx, "c") == 600 and chain.net_effect(tx, "b") == -50


# ── RBF ─────────────────────────────────────────────────────

async def test_rbf_polls_replacements_and_toggles_full_rbf(no_network):
    hub = make_hub()
    pane = await opened(chain.RbfPane, hub)
    text = page(pane, 130)
    assert pane.title == "replacements" and pane.provs[0].source == "mempool.space" and "polled every 15 s" in text
    for want in ("18:54:13", "3.01 → 3.21", "+0.20", "+6.6%", "63s", "₿830", "₿26,042,489", "2.94 → 2.98", "26"):
        assert want in text, want
    assert pane.enter() == f"TX {TX_POOL}"
    first = chain.tree_stats(fixture("mempool/replacements.json")[0])
    assert first["count"] == 1 and first["gap"] == 63 and round(first["d_rate"], 4) == 0.1998 and first["d_fee"] == 830 and not first["full"]
    assert pane.key("f", "f") and pane.full and pane.loaded_at == 0
    await pane.reload()
    assert "https://mempool.space/api/v1/fullrbf/replacements" in no_network.seen and not no_network.missing
    text = page(pane, 130)
    full = fixture("mempool/fullrbf_replacements.json")
    assert "FULL-RBF" in text and "full" in text and pane.enter() == f"TX {full[0]['tx']['txid']}" and pane.n_rows == len(full)
    header, rows = pane.export()
    assert header[0] == "txid" and rows[0][0] == full[0]["tx"]["txid"] and rows[0][10] is True


async def test_rbf_reads_the_stream_while_mounted_and_lets_go_after(no_network):
    hub = make_hub()
    pane = chain.RbfPane(hub, None, ())
    pane.on_mount()
    assert hub.stream_subscriptions() == [{"track-rbf": "all"}] and hub.stream_commands.get_nowait() == {"track-rbf": "all"}
    hub.rbf_latest = fixture("mempool/ws_rbf_latest.json")["rbfLatest"]
    await pane.reload()
    assert not [u for u in no_network.seen if "replacements" in u]                  # the stream is feeding it: no REST call
    text = page(pane, 130)
    assert "stream" in text and "3.44 → 5.27" in text and "+1.84" in text and "+53.5%" in text and "15m" in text
    assert pane.provs[0].source == "mempool.space ws" and pane.enter() == "TX e819307da6c0da69b56f542dafddef08bf370960de58a35f1acdef297e422561"
    pane.on_unmount()
    assert hub.stream_subscriptions() == [] and hub.stream_commands.get_nowait() == {"track-rbf": "stop"}


# ── every page, every width ─────────────────────────────────

async def test_no_line_is_wider_than_the_pane_and_every_glyph_is_one_cell(no_network):
    hub = make_hub()
    panes = await every_pane(hub)
    panes[5].key("w", "w")                                                          # the widest prose on any of these pages
    glyphs = set()
    for pane in panes:
        for w in WIDTHS:
            for ln in pane.draw(w, 30):
                assert cell_len(ln.plain) <= w, (pane.code, pane.args, w, ln.plain)
                assert "\n" not in ln.plain
                glyphs |= set(ln.plain)
    assert not [g for g in glyphs if cell_len(g) != 1]
    assert "—" not in glyphs


async def test_cursor_rows_follow_the_lines_they_are_drawn_on(no_network):
    hub = make_hub()
    for pane in await every_pane(hub):
        for cur in range(pane.n_rows):
            pane.cur, pane.top = cur, 0
            lines = pane.draw(100, 200)
            on = [i for i, ln in enumerate(lines) if any("on " in str(s.style) for s in ln.spans)]
            assert len(on) == 1, (pane.code, pane.args, cur, on)
            if pane.line_of:
                assert pane.line_of[cur] == on[0], (pane.code, pane.args, cur)
            assert pane.enter() is None or pane.enter().split()[0] in ("TX", "ADDR", "BLK")


async def test_a_redraw_stays_inside_the_budget(no_network):
    for pane in await every_pane(make_hub()):
        pane.draw(120, 40)
        t0 = time.perf_counter()
        for _ in range(5):
            pane.draw(120, 40)
        assert (time.perf_counter() - t0) / 5 < 0.010, pane.code


def test_the_four_functions_are_registered_with_plain_help():
    registry.load_all()
    for code in ("BLK", "TX", "ADDR", "RBF"):
        fn = registry.get(code)
        assert fn and fn.category == "Bitcoin" and fn.make and len(fn.help) > 200 and "—" not in fn.help + fn.summary
    assert registry.get("TX").args == "<txid>" and registry.get("BLK").args == "[height or hash]"


# ── through the real shell ──────────────────────────────────

async def test_the_go_bar_opens_the_pages_and_enter_walks_from_block_to_transaction(monkeypatch, no_network):
    import test_app as T

    import glimpse_tui.app as A
    from glimpse_tui import auth
    monkeypatch.setattr(A, "Glimpse", T.FakeApi)
    monkeypatch.setattr(auth, "load_key", lambda: (None, "none"))
    monkeypatch.setattr(A.Terminal, "load_spot", lambda self: None)
    app = A.Terminal(launchpad="BTC")

    async def go(pilot, text):
        await pilot.press("`")
        for ch in text:
            await pilot.press("space" if ch == " " else ch)
        await pilot.press("enter")

    def shown() -> str:
        return app.shell.ws.pane.render().plain

    async with app.run_test(size=(132, 40)) as pilot:
        await T.until(pilot, lambda: len(app.shell.ws.panes) == 4)
        hub = app.shell.hub
        for s in hub.sources.values():
            s.retries, s.bucket = 0, core.TokenBucket(1000, 1000)
        await go(pilot, "967730")                                                   # a bare height is BLK
        await T.until(pilot, lambda: app.shell.ws.pane.code == "BLK" and "SpiderPool" in shown())
        assert "block 967,730" in shown() and "mempool.space" in shown() and "hash" in shown()      # it opens at the top, on the header
        await pilot.press("j")                                                      # the cursor moves, and the view follows it down
        await T.until(pilot, lambda: "₿11,340" in shown())
        await pilot.press("enter")                                                  # the second transaction of the block
        await T.until(pilot, lambda: app.shell.ws.pane.code == "TX" and "pays 60.2 sat/vB" in shown())
        assert "This lookup goes to mempool.space" in shown()
        await go(pilot, "RBF")
        await T.until(pilot, lambda: app.shell.ws.pane.code == "RBF" and "+6.6%" in shown())
        assert hub._tracking["rbf"] == 1                                            # held while the pane is open
        await go(pilot, "BLK")
        await T.until(pilot, lambda: app.shell.ws.pane.code == "BLK" and "Foundry USA" in shown())
        assert hub._tracking["rbf"] == 0                                            # and let go when it closed


# ── privacy ─────────────────────────────────────────────────

async def test_the_privacy_line_shows_on_the_first_public_lookup_only(no_network):
    hub = make_hub()
    line = "This lookup goes to mempool.space. Use your own node or Tor to keep it private (SET)."
    blk = await opened(chain.BlkPane, hub, "967730")
    assert "This lookup" not in page(blk) and not hub.cfg["privacy_ack"]            # blocks are not personal
    first = await opened(chain.TxPane, hub, TX)
    assert not hub.cfg["privacy_ack"]                                               # nothing is recorded until it has been drawn
    assert page(first, 100).split("\n")[0] == line
    assert hub.cfg["privacy_ack"] is True and config.load()["privacy_ack"] is True
    assert line in page(first, 100)                                                 # it stays while this lookup is on screen
    await first.reload()
    assert "This lookup" not in page(first, 100)                                    # and is gone from the next load on
    assert "This lookup" not in page(await opened(chain.AddrPane, hub, ADDRESS))
    assert "This lookup" not in page(await opened(chain.TxPane, make_hub(), TX_DATA))       # a new session reads it from terminal.toml


async def test_a_private_backend_is_never_nagged_about(no_network):
    hub = make_hub()
    hub.cfg["bitcoin"].update(order=["mempool"], mempool="http://umbrel.local:3006")
    hub.apply_config()
    hub.sources["mempool"].retries = 0
    pane = chain.AddrPane(hub, None, (ADDRESS,))
    await pane.reload()
    assert pane.error and pane.notice_host == "" and not hub.cfg["privacy_ack"]     # nothing recorded there, and nothing leaked
    assert all(u.startswith("http://umbrel.local:3006/") for u in no_network.seen)


# ── the fallback chain ──────────────────────────────────────

async def test_esplora_answers_when_mempool_is_down(no_network):
    hub = make_hub()
    hub.cfg["bitcoin"]["order"] = ["mempool", "esplora"]
    no_network.fail.add("mempool.space")
    pane = await opened(chain.TxPane, hub, TX_DATA)
    text = page(pane, 110)
    assert [p.source for p in pane.provs] == ["blockstream.info"]
    assert "This lookup goes to blockstream.info." in text                          # the host that really saw the lookup
    assert "to:USDT(TRON):TNszJgNMfqyoqVup3oYyJHVWEvWck7bYkS" in text and "6.21 sat/vB" in text and "unspent" in text
    assert "confirmations 2" in text
    assert "CPFP" not in text and "REPLACEMENTS" not in text                        # Esplora has neither, and the page holds
    addr = await opened(chain.AddrPane, hub, ADDRESS)
    assert "₿44,405,441" in page(addr) and "₿27,323" in page(addr) and addr.provs[0].source == "blockstream.info"
    blk = await opened(chain.BlkPane, hub, BLOCK)
    text = page(blk, 100)
    assert "3,967 tx" in text and "132.76 T" in text and "FEE RATES" not in text and "did not return the transactions" in text
    lst = await opened(chain.BlkPane, hub)
    assert "967,731" in page(lst) and "no pool, fee or audit fields" in page(lst)


async def test_ask_names_the_backend_and_remembers_an_unreachable_one(no_network):
    hub = make_hub()
    via: list[str] = []
    height, prov = await chain.ask(hub, "tip_height", via=via)
    assert height == 967731 and prov.source == "mempool.space" and via == ["mempool"]
    with pytest.raises(core.SourceError, match="no Bitcoin backend answers"):
        await chain.ask(hub, "no_such_capability")

    async def unreachable(*a):
        raise core.SourceError("mempool.space: ConnectError")
    hub.sources["mempool"].tip_height = unreachable
    hub.cfg["bitcoin"]["order"] = ["mempool", "esplora"]
    skip: set[str] = set()
    height, prov = await chain.ask(hub, "tip_height", skip=skip)
    assert height == 967731 and prov.source == "blockstream.info" and skip == {"mempool"}
    fees, prov = await chain.ask(hub, "fees", skip=skip)                            # mempool is not asked again this load
    assert prov.source == "blockstream.info" and not [u for u in no_network.seen if "fees/recommended" in u]


async def test_esplora_speaks_the_mempool_shapes(no_network):
    es = esplora.Esplora()
    es.bucket = core.TokenBucket(1000, 1000)
    assert es.base_url == "https://blockstream.info" and es.name == "blockstream.info"
    fees, prov = await es.fees()
    assert fees == {"fastestFee": 3, "halfHourFee": 3, "hourFee": 2, "economyFee": 1, "minimumFee": 1} and prov.source == "blockstream.info"
    precise, _ = await es.fees_precise()
    assert precise == {"fastestFee": 2.106, "halfHourFee": 2.106, "hourFee": 1.193, "economyFee": 0.277, "minimumFee": 0.1}
    assert (await es.tip_height())[0] == 967731
    assert (await es.block(BLOCK))[0]["tx_count"] == 3967 and (await es.blocks())[0][0]["height"] == 967731
    assert (await es.tx(TX_DATA))[0]["fee"] == 1000 and (await es.outspends(TX_DATA))[0] == [{"spent": False}, {"spent": False}]
    assert (await es.address(ADDRESS))[0]["chain_stats"]["funded_txo_sum"] == 44405441
    assert len((await es.address_utxo(ADDRESS))[0]) == 5 and "fee_histogram" in (await es.mempool())[0]
    assert all(u.startswith("https://blockstream.info/api/") for u in no_network.seen) and not no_network.missing
    assert not hasattr(es, "cpfp") and not hasattr(es, "rbf") and not hasattr(es, "mempool_blocks")
    assert esplora.recommended({}) == {k: 1 for k, _ in esplora.TARGETS}


# ── Electrum ────────────────────────────────────────────────

class Replay:
    """A recorded Electrum session as a fake reader and writer. Every line the client sends must equal the recorded
    one; the recorded reply is then handed back byte for byte, with a notification slipped in front of it."""

    def __init__(self, name: str) -> None:
        rows = [json.loads(ln) for ln in (ROOT / "electrum" / name).read_text().splitlines() if ln.strip()]
        self.meta = rows[0]["_meta"]
        self.script = [r for r in rows[1:] if r.get("dir")]
        self.inbox: list[bytes] = []
        self.sent: list[dict] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        msg = json.loads(data)
        want = self.script.pop(0)
        assert want["dir"] == "send" and want["msg"] == msg, (want, msg)
        self.sent.append(msg)
        self.inbox.append(json.dumps({"jsonrpc": "2.0", "method": "blockchain.headers.subscribe", "params": [{"height": 1}]}).encode() + b"\n")
        self.inbox.append(json.dumps(self.script.pop(0)["msg"]).encode() + b"\n")

    async def drain(self) -> None:
        pass

    async def readline(self) -> bytes:
        return self.inbox.pop(0) if self.inbox else b""

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize(("name", "server", "fee2", "fee25"), [("electrum_blockstream_info.jsonl", "electrs-esplora 0.4.1", 2.106, 0.382),
                                                                ("bitcoin_lu_ke.jsonl", "ElectrumX 1.18.0", 2.12, 1.0)])
async def test_electrum_replays_the_recorded_transcript(name, server, fee2, fee25):
    wire = Replay(name)
    opened_with: list[str] = []

    async def opener(url, timeout):
        opened_with.append(url)
        return wire, wire
    el = electrum.Electrum(f"ssl://{wire.meta['host']}:{wire.meta['port']}", opener=opener)
    banner, prov = await el.call("server.banner")                                   # connecting sends server.version first, as recorded
    assert el.server == server and isinstance(banner, str) and prov.source == wire.meta["host"] and prov.delay == "live"
    features, _ = await el.call("server.features")
    assert features["genesis_hash"] == "000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f"
    assert (await el.tip_height())[0] == 967731                                     # the unsolicited notification was skipped
    hist, _ = await el.fee_histogram()
    assert hist[0][0] > hist[-1][0] >= 0 and all(len(row) == 2 for row in hist)
    assert (await el.estimate_fee(2))[0] == fee2 and (await el.estimate_fee(25))[0] == fee25       # BTC per kB into sat/vB
    assert (await el.call("blockchain.relayfee"))[0] > 0 and (await el.call("server.ping"))[0] is None
    assert not wire.script and opened_with == [el.base_url] and el.health.ok == 8 and el.health.state == "ok"
    assert [m["method"] for m in wire.sent][:2] == ["server.version", "server.banner"] and wire.sent[0]["params"] == ["glimpse-tui/0.1", "1.4"]
    await el.close()
    assert wire.closed


async def test_electrum_is_only_ever_a_configured_backend():
    async def opener(url, timeout):
        raise AssertionError("an unconfigured client must not open a socket")
    el = electrum.Electrum(opener=opener)
    with pytest.raises(core.SourceError, match="no server configured"):
        await el.tip_height()
    assert not el.public and el.host == ""
    assert electrum.Electrum("ssl://electrum.example.org:50002").public and not electrum.Electrum("tcp://192.168.1.20:50001").public
    assert not electrum.Electrum("tcp://abcdefghijklmnop.onion:50001").public and not electrum.Electrum("ssl://127.0.0.1").public
    with pytest.raises(core.SourceError, match="must look like"):
        electrum.parse_url("https://example.org")
    assert electrum.parse_url("ssl://h") == ("ssl", "h", 50002) and electrum.parse_url("tcp://h") == ("tcp", "h", 50001)


def test_electrum_trusts_a_self_signed_certificate_only_when_the_url_says_so():
    strict, loose = electrum.tls_context("ssl"), electrum.tls_context("ssl+insecure")
    assert strict.verify_mode == ssl.CERT_REQUIRED and strict.check_hostname
    assert loose.verify_mode == ssl.CERT_NONE and not loose.check_hostname
    assert electrum.tls_context("tcp") is None


async def test_electrum_reports_errors_and_reconnects_after_a_drop():
    class Wire:
        def __init__(self, replies):
            self.replies, self.closed = replies, False

        def write(self, data):
            self.last = json.loads(data)

        async def drain(self):
            pass

        async def readline(self):
            r = self.replies.pop(0)
            return b"" if r is None else json.dumps({"jsonrpc": "2.0", "id": self.last["id"], **r}).encode() + b"\n"

        def close(self):
            self.closed = True

    wires = [Wire([{"result": ["x 1", "1.4"]}, {"error": {"code": 1, "message": "history too large"}}, None]),
             Wire([{"result": ["x 1", "1.4"]}, {"result": 7}])]
    seq = list(wires)

    async def opener(url, timeout):
        return (w := seq.pop(0)), w
    el = electrum.Electrum("tcp://127.0.0.1:50001", opener=opener)
    with pytest.raises(core.SourceError, match="127.0.0.1: history too large"):
        await el.get_history(ADDRESS)
    assert wires[0].last["params"] == [electrum.scripthash(ADDRESS)]
    with pytest.raises(core.SourceError, match="ConnectionError"):
        await el.call("server.ping")                                                # the server hung up
    assert wires[0].closed and el.health.state == "down"
    assert (await el.call("server.ping"))[0] == 7 and not seq                       # a fresh connection, version first


async def test_electrum_goes_through_the_socks5_proxy_by_name():
    class Proxy:
        def __init__(self):
            self.out, self.inbox = b"", [b"\x05\x00", b"\x05\x00\x00\x01", b"\x00" * 6]

        def write(self, data):
            self.out += data

        async def drain(self):
            pass

        async def readexactly(self, n):
            got = self.inbox.pop(0)
            assert len(got) == n
            return got
    p = Proxy()
    await electrum.socks5_connect(p, p, "abcdefghijklmnop.onion", 50001)
    assert p.out == b"\x05\x01\x00" + b"\x05\x01\x00\x03" + bytes([22]) + b"abcdefghijklmnop.onion" + (50001).to_bytes(2, "big")
    refused = Proxy()
    refused.inbox = [b"\x05\x00", b"\x05\x05\x00\x01"]
    with pytest.raises(core.SourceError, match="code 5"):
        await electrum.socks5_connect(refused, refused, "example.org", 50002)


def test_scripthash_matches_the_protocol_documentation_and_the_recorded_scripts():
    assert electrum.scripthash("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa") == "8b01df4e368ea28f8dc0423bcf7a4923e3a12d307c875e47a0cfbf90b5c39161"
    seen = 0
    for name in ("tx.json", "tx_opreturn.json", "tx_replacement_mempool.json"):
        tx = fixture(f"mempool/{name}")
        for o in [i["prevout"] for i in tx["vin"]] + tx["vout"]:
            if a := o.get("scriptpubkey_address"):                                  # p2wsh, p2wpkh, p2tr and p2pkh are all in there
                assert electrum.script_pubkey(a).hex() == o["scriptpubkey"], a
                seen += 1
    assert seen >= 7
    assert electrum.script_pubkey("3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy").hex() == "a914b472a266d0bd89c13706a4132ccfb16f7c3b9fcb87"
    assert electrum.scripthash(bytes.fromhex("6a")) == electrum.scripthash(b"\x6a")
    for bad in ("bc1qxdn2rz8a76kcdqz47yg788tzh06nl7ujc2k4v7", "12cbQLTFMXRnSzktFkuoG3eHoMeFtpTu3T", "hello", ""):
        with pytest.raises(ValueError, match="not a Bitcoin address"):
            electrum.script_pubkey(bad)


# ── Bitcoin Core RPC ────────────────────────────────────────

async def test_core_rpc_builds_json_rpc_and_keeps_credentials_out_of_every_message(no_network):
    calls: list[httpx.Request] = []

    def node(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        body = json.loads(request.content)
        if request.headers.get("authorization") != "Basic " + base64.b64encode(b"satoshi:hunter2pw").decode():
            return httpx.Response(401, text="")
        results = {"getblockchaininfo": {"chain": "main", "blocks": 967731}, "getblockhash": BLOCK, "getmempoolinfo": {"size": 4},
                   "getblock": {"hash": BLOCK}, "getrawtransaction": {"txid": TX}}
        if body["method"] == "estimatesmartfee":                                    # a young node has no estimate for the 1008-block target
            target = body["params"][0]
            results["estimatesmartfee"] = {"feerate": 0.00002106, "blocks": target} if target < 1000 else {"errors": ["none"]}
        if body["method"] not in results:
            return httpx.Response(404, json={"result": None, "error": {"code": -32601, "message": "Method not found"}, "id": body["id"]})
        return httpx.Response(200, json={"result": results[body["method"]], "error": None, "id": body["id"]})

    http.use_transport(httpx.MockTransport(node))
    rpc = core_rpc.CoreRpc("http://satoshi:hunter2pw@127.0.0.1:8332/")
    assert rpc.base_url == "http://127.0.0.1:8332" and rpc.host == "127.0.0.1:8332" and "hunter2pw" not in repr(rpc) + rpc.base_url
    info, prov = await rpc.getblockchaininfo()
    assert info["blocks"] == 967731 and prov.source == "bitcoin core" and (await rpc.tip_height())[0] == 967731
    first = json.loads(calls[0].content)
    assert first == {"jsonrpc": "1.0", "id": 1, "method": "getblockchaininfo", "params": []} and calls[0].method == "POST"
    assert str(calls[0].url) == "http://127.0.0.1:8332/" and calls[0].headers["content-type"] == "application/json"
    assert (await rpc.block_hash(967730))[0] == BLOCK and json.loads(calls[-1].content)["params"] == [967730]
    await rpc.getblock(BLOCK)
    assert json.loads(calls[-1].content)["params"] == [BLOCK, 1]
    await rpc.getblock(BLOCK, 2)
    assert json.loads(calls[-1].content)["params"] == [BLOCK, 2]
    await rpc.getrawtransaction(TX)
    assert json.loads(calls[-1].content)["params"] == [TX, 1]
    await rpc.getrawtransaction(TX, block_hash=BLOCK)
    assert json.loads(calls[-1].content)["params"] == [TX, 1, BLOCK]
    await rpc.getmempoolinfo()
    await rpc.estimatesmartfee(6)
    assert json.loads(calls[-1].content) | {"id": 0} == {"jsonrpc": "1.0", "id": 0, "method": "estimatesmartfee", "params": [6, "economical"]}
    fees, _ = await rpc.fees()
    assert fees == {"fastestFee": 2.106, "halfHourFee": 2.106, "hourFee": 2.106, "economyFee": 2.106, "minimumFee": 1.0}
    with pytest.raises(core.SourceError, match=r"no_such failed \(-32601\): Method not found") as e:
        await rpc.call("no_such")
    assert "hunter2pw" not in str(e.value) and "satoshi" not in str(e.value) and "hunter2pw" not in rpc.health.last_error

    wrong = core_rpc.CoreRpc("http://satoshi:wr0ngpass@127.0.0.1:8332")
    with pytest.raises(core.SourceError, match="HTTP 401") as e:
        await wrong.getmempoolinfo()
    assert "wr0ngpass" not in str(e.value) and "satoshi" not in str(e.value) and "wr0ngpass" not in wrong.health.last_error

    def broken(request):
        raise httpx.ConnectError(f"cannot reach {request.url} with {request.headers.get('authorization')}")
    http.use_transport(httpx.MockTransport(broken))
    with pytest.raises(core.SourceError) as e:
        await rpc.getmempoolinfo()
    assert str(e.value) == "bitcoin core: ConnectError"                             # the type and nothing the exception carried


async def test_core_rpc_reads_the_cookie_fresh_and_refuses_without_a_node(no_network, tmp_path):
    seen: list[str] = []

    def node(request):
        seen.append(base64.b64decode(request.headers["authorization"].split()[1]).decode())
        return httpx.Response(200, json={"result": {"blocks": 1}, "error": None, "id": 1})
    http.use_transport(httpx.MockTransport(node))
    cookie = tmp_path / ".cookie"
    cookie.write_text("__cookie__:abc123\n")
    rpc = core_rpc.CoreRpc("http://127.0.0.1:8332", cookie=cookie)
    await rpc.getblockchaininfo()
    cookie.write_text("__cookie__:def456\n")                                        # the node restarted
    await rpc.getblockchaininfo()
    assert seen == ["__cookie__:abc123", "__cookie__:def456"]
    missing = core_rpc.CoreRpc("http://127.0.0.1:8332", cookie=tmp_path / "nope")
    with pytest.raises(core.SourceError, match="no cookie file"):
        await missing.getblockchaininfo()
    with pytest.raises(core.SourceError, match="no node configured"):
        await core_rpc.CoreRpc().getblockchaininfo()
    assert len(seen) == 2                                                           # neither of those sent anything
