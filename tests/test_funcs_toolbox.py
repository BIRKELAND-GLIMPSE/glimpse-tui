"""AL, NOTE, CALC, ASK and EXP."""
import csv
import time

import pytest
from offline import fixture

from glimpse_tui.data import feeds, quotes
from glimpse_tui.funcs import toolbox, tools
from glimpse_tui.funcs.btc import MempPane
from glimpse_tui.term import alerts, gobar, registry
from glimpse_tui.term.hub import Hub


def text(lines) -> str:
    return "\n".join(ln.plain for ln in lines)


# ── AL ──────────────────────────────────────────────────────

def test_alert_rules_parse_from_plain_words():
    r = alerts.parse("BTC below 75,000")
    assert (r.kind, r.subject, r.op, r.level) == ("price", "BTC", "below", 75_000)
    assert alerts.parse("xau above 4.5k").level == 4500 and alerts.parse("next-block fee under 3 sat/vB").kind == "fee"
    assert alerts.parse("mempool over 120").level == 120 and alerts.parse("a block from Foundry USA").subject == "foundry usa"
    assert alerts.parse("block").subject == "" and alerts.parse("difficulty").kind == "difficulty"
    f = alerts.parse("MSTR files an 8-K")
    assert (f.kind, f.subject, f.op) == ("filing", "MSTR", "8-K")
    o = alerts.parse("odds on 80,000 to 82,000 at the 16:00 close above 20%")
    assert (o.kind, o.lo, o.hi, o.when, o.op, o.level) == ("odds", 80_000, 82_000, "16:00", "above", 0.20)
    with pytest.raises(ValueError, match="Try:"):
        alerts.parse("wake me up when it moons")


async def test_a_level_alert_fires_once_and_rearms_and_a_block_alert_fires_on_each_block(no_network):
    hub = Hub()
    await feeds.poll_chain_once(hub)
    await quotes.refresh(hub, ["BTC"])
    eng = alerts.Engine()
    eng.add("BTC above 80000")
    eng.add("fee under 1")
    eng.add("block from foundry")
    seen = len(no_network.seen)
    fired = eng.evaluate(hub)
    assert len(fired) == 1 and "BTC is above 80,000" in fired[0] and "bitstamp+coinbase+kraken" in fired[0]
    assert eng.evaluate(hub) == []                                          # still true: it does not fire again
    hub.quotes["BTC"].price = 79_000
    assert eng.evaluate(hub) == [] and eng.rules[0].armed                   # false again: re-armed
    hub.quotes["BTC"].price = 80_500
    assert len(eng.evaluate(hub)) == 1
    block = dict(fixture("mempool/blocks.json")[0], height=hub.chain.height + 1)
    feeds.apply_ws(hub, {"block": block})
    assert "mined by Foundry USA" in eng.evaluate(hub)[0]
    assert len(no_network.seen) == seen                                     # evaluation never makes a request
    assert [r.text for r in alerts.Engine().rules] == ["BTC above 80000", "fee under 1", "block from foundry"]      # kept on disk
    assert hub.events[-1]["kind"] == "alert"


async def test_al_page_adds_from_the_go_bar_and_lists_rules():
    hub = Hub()
    pane = toolbox.AlPane.from_command(hub, gobar.parse("AL BTC below 75000", hub.book))
    out = text(pane.draw(100, 20))
    assert "BTC below 75000" in out and "armed" in out and "Added" in out
    bad = toolbox.AlPane(hub, None, ("make", "me", "rich"))
    assert "Try:" in text(bad.draw(100, 20)) and len(toolbox.engine().rules) == 1
    pane.key("x", "x")
    assert not toolbox.engine().rules


# ── NOTE, CALC, ASK ─────────────────────────────────────────

async def test_notes_attach_to_a_security_and_stay_on_disk():
    hub = Hub()
    toolbox.NotePane.from_command(hub, gobar.parse("NOTE BTC watching the 200-day average", hub.book))
    toolbox.NotePane(hub, None, ("general", "thought"))
    btc = toolbox.NotePane.from_command(hub, gobar.parse("NOTE BTC", hub.book))
    assert "watching the 200-day average" in text(btc.draw(100, 10)) and "general thought" not in text(btc.draw(100, 10))
    every = toolbox.NotePane(hub)
    assert "general thought" in text(every.draw(100, 10)) and "BTC" in text(every.draw(100, 10))
    btc.key("x", "x")
    assert "No notes yet" in text(btc.draw(100, 10)) and btc.command() == "NOTE BTC"


def test_calc():
    calc = lambda s, p=80_000.0: dict(toolbox.calculate(s.split(), p))      # noqa: E731
    assert calc("50000")["dollars"] == "$40.00" and calc("0.015 btc")["sats"] == "₿1,500,000"
    assert calc("$25")["sats"] == "₿31,250" and calc("fee 140.5 12")["fee"] == "₿1,686"
    assert calc("tx 2 2 p2wpkh 12")["size"] == "208.5 vB"
    k = calc("kelly 0.6 2.5 200000")
    assert k["full Kelly"].startswith("33.3%") and k["break-even"] == "40.0%" and "₿16,667" in k["stake"]
    assert "no bet" in calc("kelly 0.3 2")["full Kelly"]
    with pytest.raises(ValueError):
        toolbox.calculate(["nonsense"], 80_000)
    with pytest.raises(ValueError, match="No BTC price"):
        toolbox.calculate(["$25"], None)


def test_ask_routes_words_and_always_shows_the_command():
    registry.load_all()
    book = Hub().book
    r = lambda s: toolbox.route(s, book)[0]                                  # noqa: E731
    assert r("how full is the mempool") == "MEMP" and r("what does it cost to send a transaction, fee wise") == "FEES"
    assert r("when is the halving") == "HALV" and r("show me the yield curve") == "RATES"
    assert r("chart gold against the s&p") .startswith("GP") and "XAU" in r("chart XAU against SPX")
    assert r("a" * 64).startswith("TX ") and r("purple monkey dishwasher") == ""
    pane = toolbox.AskPane(Hub(), None, tuple("how full is the mempool".split()))
    import asyncio
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(pane.load())
    out = text(pane.draw(100, 20))
    assert "I read that as" in out and "MEMP" in out and "Nothing runs until you press it" in out and pane.enter() == "MEMP"


# ── EXP ─────────────────────────────────────────────────────

async def test_exp_writes_the_panes_table_with_its_sources(no_network, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    hub = Hub()
    await feeds.poll_chain_once(hub)
    pane = MempPane(hub)
    await pane.reload()
    pane.draw(100, 30)
    msg = tools.export_pane(pane)
    files = list((tmp_path / "Downloads" / "glimpse").glob("MEMP_*.csv"))
    assert "Exported" in msg and len(files) == 1
    rows = list(csv.reader(files[0].open()))
    assert rows[0][:3] == ["block", "vsize", "txs"] and rows[1][2] == str(fixture("mempool/fees_mempool_blocks.json")[0]["nTx"])
    assert rows[-1][0] == "# sources" and "mempool.space" in rows[-1][1]
    assert "no table" in tools.export_pane(toolbox.CalcPane(hub)) and "Open a launchpad" in tools.export_pane(None)
    assert time.time() > 0
