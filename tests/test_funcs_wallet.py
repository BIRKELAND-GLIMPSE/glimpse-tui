"""WAL, OMON and GIV. barkd is a fake httpx transport shaped by its OpenAPI; nothing here can move funds."""
import json
import math
import time

import httpx
import pytest
import test_app as T
from offline import ROOT
from rich.cells import cell_len

import glimpse_tui.app as A
from glimpse_tui import auth
from glimpse_tui.funcs import options, wallet
from glimpse_tui.term import config
from glimpse_tui.term.hub import Hub
from glimpse_tui.wallet import barkd as B
from glimpse_tui.wallet import watch as W

TOKEN = "AQIDBAUGBwgJCgsMDQ4PEBESExQVFhcYGRobHB0eHyA"         # a made-up token for the fake daemon
ZPUB = "zpub6rFR7y4Q2AijBEqTUquhVz398htDFrtymD9xYYfG1m4wAcvPhXNfE3EfH1r1ADqtfSdVCToUG868RvUUkgDKf31mGDtKsAYz2oz2AGutZYs"   # BIP84's test vector
INVOICE = "lntbs500u1p5testpp5qqqsyqcyq5rqwzqfqqqsyqcyq5rqwzqfqqqsyqcyq5rqwzqfqypqdq5xysxxatsyp3k7enxv4jsxqzpu"


class FakeBarkd:
    """barkd 0.7.1's routes, from the recorded OpenAPI. Records every call; refuses a wrong token."""

    def __init__(self, network="signet", spendable=1_250_000):
        self.calls: list[tuple[str, str, dict]] = []
        self.network, self.spendable = network, spendable
        self.paths = json.loads((ROOT / "barkd" / "openapi.json").read_text())["paths"]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        assert path in self.paths or path == "/api/v1/ping", f"{path} is not in barkd's OpenAPI"
        if request.headers.get("authorization") != f"Bearer {TOKEN}" and not path.endswith("/ping"):
            return httpx.Response(401, json={"message": "unauthorized"})
        body = json.loads(request.content) if request.content else {}
        self.calls.append((request.method, path.removeprefix("/api/v1"), body or dict(request.url.params)))
        p = path.removeprefix("/api/v1")
        if p == "/wallet/balance":
            return httpx.Response(200, json={"spendable_sat": self.spendable, "pending_in_round_sat": 0, "pending_lightning_send_sat": 0,
                                             "claimable_lightning_receive_sat": 0, "pending_board_sat": 5000, "pending_exit_sat": None})
        if p == "/onchain/balance":
            return httpx.Response(200, json={"total_sat": 310_000, "confirmed_sat": 310_000, "trusted_spendable_sat": 310_000,
                                             "trusted_pending_sat": 0, "untrusted_pending_sat": 0, "immature_sat": 0})
        if p == "/wallet/ark-info":
            return httpx.Response(200, json={"network": self.network})
        if p == "/wallet/vtxos":
            return httpx.Response(200, json=[{"id": "v1", "amount_sat": 1_250_000,
                                              "expiry_height": 250_000 + 21 * 144, "state": {"type": "spendable"}}])
        if p == "/bitcoin/tip":
            return httpx.Response(200, json={"tip_height": 250_000})
        if p in ("/boards/pending", "/exits/status/all", "/history"):
            return httpx.Response(200, json=[])
        if p == "/lightning/receives/invoice":
            return httpx.Response(200, json={"invoice": INVOICE})
        if p == "/wallet/addresses/next":
            return httpx.Response(200, json={"address": "tark1qexampleexampleexampleexampleexample"})
        if p == "/fees/lightning/pay":
            return httpx.Response(200, json={"fee_sat": 12, "gross_amount_sat": 50_012, "net_amount_sat": 50_000, "vtxos_spent": ["v1"]})
        if p == "/wallet/send":
            return httpx.Response(200, json={"message": "payment sent", "payment_hash": "ab" * 32})
        return httpx.Response(404, json={"message": "not faked"})


# ── the barkd client and the send rules ─────────────────────

async def test_client_reads_balances_and_never_leaks_the_token():
    fake = FakeBarkd()
    bark = B.Barkd("http://127.0.0.1:3000", TOKEN, httpx.MockTransport(fake))
    bal = await bark.balance()
    assert (bal.spendable, bal.pending, bal.onchain_total) == (1_250_000, 5000, 310_000) and await bark.network() == "signet"
    bad = B.Barkd("http://127.0.0.1:3000", "wrong", httpx.MockTransport(fake))
    with pytest.raises(B.BarkdError) as e:
        await bad.balance()
    assert "token" in str(e.value) and "wrong" not in str(e.value) and TOKEN not in str(e.value)
    with pytest.raises(B.BarkdError, match="never asks barkd for the seed"):
        await bark._call("GET", "/wallet/mnemonic")
    with pytest.raises(B.BarkdError, match="never deletes"):
        await bark._call("DELETE", "/wallet")
    assert not any("mnemonic" in p for _, p, _ in fake.calls)
    assert not hasattr(bark, "mnemonic") and not hasattr(bark, "delete_wallet")


def test_destinations_amounts_and_networks():
    assert B.kind_of(INVOICE) == "lightning" and B.kind_of("tark1q" + "x" * 30) == "ark" and B.kind_of("bc1q" + "w" * 38) == "onchain"
    assert B.kind_of("hello") == "" and B.kind_of("sat@example.com") == "lnaddress"
    assert B.invoice_amount_sat(INVOICE) == 50_000 and B.invoice_amount_sat("lnbc1p" + "q" * 20) is None
    assert B.invoice_amount_sat("lnbc2500u1" + "q" * 20) == 250_000 and B.invoice_amount_sat("lnbc20m1" + "q" * 20) == 2_000_000
    assert B.network_of(INVOICE) == "signet/testnet" and B.network_of("bc1qabc") == "mainnet" and B.network_of("bcrt1qabc") == "regtest"


def test_every_send_rule_is_checked_before_any_confirmation():
    ok = dict(destination=INVOICE, amount_sat=50_000, fee_sat=12, spendable=1_000_000, cap=1_000_000, daemon_network="signet")
    assert B.check_send(**ok) == ""
    assert "not a Lightning invoice" in B.check_send(**{**ok, "destination": "nonsense"})
    assert "invoice asks for" in B.check_send(**{**ok, "amount_sat": 60_000})
    assert "more than" in B.check_send(**{**ok, "spendable": 50_005})
    assert "barkd is on bitcoin" in B.check_send(**{**ok, "daemon_network": "bitcoin"})          # a signet invoice, a mainnet daemon
    assert "barkd is on signet" in B.check_send(**{**ok, "destination": "bc1q" + "w" * 38})
    B.record_sent(980_000)
    assert "daily send cap" in B.check_send(**ok) and B.sent_today() == 980_000
    assert B.sent_today(time.time() + 86400) == 0                           # a new UTC day, a new allowance


# ── watch-only ──────────────────────────────────────────────

def test_watch_only_derives_the_bip84_vector_and_refuses_private_keys():
    w = W.parse(f"cold={ZPUB}")
    assert w.label == "cold" and w.network == "main" and w.descriptor.startswith("wpkh(xpub")
    assert W.addresses(w, 0, 0, 2) == ["bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu", "bc1qnjg0jd8228aq7egyzacy8cys3knf9xvrerkf9g"]
    assert W.addresses(w, 1, 0, 1) == ["bc1q8c6fshw2dlwun7ekn9qwf37cu2rn755upcp6el"]
    d = W.parse(f"wpkh({w.descriptor[5:-1].replace('/<0;1>/*', '')}/<0;1>/*)")
    assert W.addresses(d, 0, 0, 1) == W.addresses(w, 0, 0, 1)
    for bad in ("xprv9s21ZrQH143K3QTDL4LXw2F7HEK3wJUD2nW2nRk4stbPy6cq3jPPqjiChkVvvNKmPGJxWUtg6LnF5kejMRNNU3TGtRBeJgk33yuGBxrMPHi",
                "wpkh(xprv9s21ZrQH143K3QTDL4LXw2F7HEK3wJUD2nW2nRk4stbPy6cq3jPPqjiChkVvvNKmPGJxWUtg6LnF5kejMRNNU3TGtRBeJgk33yuGBxrMPHi/0/*)"):
        with pytest.raises(W.WatchError, match="private key"):
            W.parse(bad)
    with pytest.raises(W.WatchError):
        W.parse("not a key")


async def test_derived_addresses_never_reach_a_public_backend_without_consent(no_network):
    hub = Hub()
    w = W.parse(ZPUB)
    with pytest.raises(W.WatchError, match="never go to a public backend"):
        await W.scan(hub, w)
    assert not [u for u in no_network.seen if "/address/" in u]
    hub.cfg["wallet"]["watch_public_ok"] = True
    hub.sources["mempool"].rate = 1000                                      # the test does not need to be polite to a mock
    hub.sources["mempool"].bucket.rate = 1000
    sc = await W.scan(hub, w)
    assert sc.scanned >= 40 and sc.backend == "mempool.space"               # the recorded address answers for every lookup
    asked = [u for u in no_network.seen if "/address/" in u and not u.endswith("/txs")]
    assert asked[0].endswith("bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu")


# ── WAL ─────────────────────────────────────────────────────

async def test_wal_draws_three_wallets_and_a_qr_code(no_network, monkeypatch):
    hub = Hub()
    pane = wallet.WalPane(hub)
    await pane.reload()
    out = "\n".join(ln.plain for ln in pane.draw(110, 40))
    assert "GLIMPSE ACCOUNT" in out and "read-only" in out and "cannot withdraw" in out
    assert "ARK WALLET" in out and "no daemon configured" in out and "holds the seed" in out
    assert "WATCH-ONLY" in out and "Public keys only" in out
    hub.cfg["wallet"]["barkd"] = "http://127.0.0.1:3000"
    pane.bark = B.Barkd("http://127.0.0.1:3000", TOKEN, httpx.MockTransport(FakeBarkd()))
    await pane.reload()
    pane.receive = ("lightning", await pane.bark.invoice(50_000), 50_000)
    out = "\n".join(ln.plain for ln in pane.draw(110, 60))
    assert "spendable ₿1,250,000" in out and "on-chain ₿310,000" in out and "signet" in out and "next VTXO expiry in 21 days" in out
    assert "RECEIVE · Lightning invoice · ₿50,000" in out and "▀" in out and INVOICE[:20] in out
    assert all(cell_len(ln.plain) <= 60 for ln in pane.draw(60, 40))
    assert TOKEN not in out


@pytest.fixture
def make(monkeypatch):
    def _make():
        monkeypatch.setattr(A, "Glimpse", T.FakeApi)
        monkeypatch.setattr(auth, "load_key", lambda: (None, "none"))
        monkeypatch.setattr(A.Terminal, "load_spot", lambda self: None)
        return A.Terminal(launchpad="BTC", go="WAL")
    return _make


async def test_a_send_needs_the_fee_the_amount_typed_back_and_a_final_yes(make):
    app = make()
    async with app.run_test(size=(132, 40)) as pilot:
        await T.until(pilot, lambda: app.shell.ws.pane and app.shell.ws.pane.code == "WAL")
        pane, fake = app.shell.ws.pane, FakeBarkd()
        app.shell.hub.cfg["wallet"]["barkd"] = "http://127.0.0.1:3000"
        pane.bark = B.Barkd("http://127.0.0.1:3000", TOKEN, httpx.MockTransport(fake))
        await pane.reload()

        async def answer(text):
            await T.until(pilot, lambda: len(app.screen_stack) > 1)
            for ch in text:
                await pilot.press(ch)
            await pilot.press("enter")
            await pilot.pause(0.05)

        sends = lambda: [c for c in fake.calls if c[1] == "/wallet/send"]   # noqa: E731
        await pilot.press("s")
        await answer(INVOICE)
        await answer("49999")                                               # typed back wrong
        await T.until(pilot, lambda: "did not match" in pane.busy)
        assert not sends()
        await pilot.press("s")
        await answer(INVOICE)
        await answer("50000")
        await T.until(pilot, lambda: isinstance(app.screen, A.Confirm))
        assert ("GET", "/fees/lightning/pay", {"amount_sat": "50000"}) in fake.calls      # the fee was asked for before any yes
        await pilot.press("n")
        await T.until(pilot, lambda: "Cancelled" in pane.busy)
        assert not sends() and B.sent_today() == 0
        await pilot.press("s")
        await answer(INVOICE)
        await answer("50000")
        await T.until(pilot, lambda: isinstance(app.screen, A.Confirm))
        await pilot.press("y")
        await T.until(pilot, lambda: pane.busy.startswith("Sent"))
        assert len(sends()) == 1 and sends()[0][2] == {"destination": INVOICE, "amount_sat": 50_000} and B.sent_today() == 50_000


# ── OMON and GIV ────────────────────────────────────────────

def test_the_lognormal_fit_recovers_a_known_distribution():
    m, s = math.log(80_000), 0.02
    bins = [(60_000 + 100 * i, 60_100 + 100 * i) for i in range(400)]
    probs = [options.ncdf((math.log(hi) - m) / s) - options.ncdf((math.log(lo) - m) / s) for lo, hi in bins]
    f = options.fit(bins, probs, 86400)
    assert f.m == pytest.approx(m, abs=1e-4) and f.s == pytest.approx(s, rel=0.01)
    assert f.iv == pytest.approx(s * math.sqrt(365.25), rel=0.01)
    assert options.above(bins, probs, 80_000) == pytest.approx(0.5, abs=0.01) == pytest.approx(f.above(80_000), abs=0.01)
    delta, _gamma, vega, theta = f.greeks(81_000, 80_000)
    # an out-of-the-money digital call gains from spot and vol, loses to time
    assert delta > 0 and vega > 0 and theta < 0
    assert f.greeks(79_000, 80_000)[2] < 0                                  # an in-the-money one loses from vol
    ks = options.strikes(bins, probs, 12)
    assert 8 <= len(ks) <= 16 and all(b > a for a, b in zip(ks, ks[1:], strict=False))


async def test_omon_hands_a_box_to_the_heatmap_and_never_orders(make):
    app = make()
    async with app.run_test(size=(150, 44)) as pilot:
        await T.ready(app, pilot)
        app.shell.run("OMON")
        await T.until(pilot, lambda: app.shell.ws.pane.code == "OMON" and app.shell.ws.pane._ks)
        pane = app.shell.ws.pane
        await pilot.press("ctrl+w", "o")                                    # zoomed: wide enough for the Greeks
        await pilot.pause(0.2)
        out = pane.render().plain
        assert "implied vol" in out and "call ₿" in out and "put ₿" in out and "Δ /$1k" in out
        await pilot.press("]", "j", "j")
        await pilot.pause(0.2)
        pane.render()                                                       # the strikes are those of the picture on screen
        at, ks = pane._at(), pane._ks
        await pilot.press("enter")
        assert app.view == "heatmap" and app.hm_selection == (1, 1, ks[at], ks[at + 1] - 1)
        assert not app.api.bought and not app.screen_stack[1:]              # a selection, not an order: the slip and Confirm come next
        assert "OMON handed this range" in app.flash
        app.shell.run("GIV")
        await T.until(pilot, lambda: app.shell.ws.pane.code == "GIV" and app.shell.ws.pane.curve)
        await T.until(pilot, lambda: "nearest" in app.shell.ws.pane.render().plain)       # drawn once the pane has its size


def test_secrets_are_not_in_the_config_file():
    config.save_secret("barkd", TOKEN)
    config.save(config.load())
    assert TOKEN not in config.path().read_text()
