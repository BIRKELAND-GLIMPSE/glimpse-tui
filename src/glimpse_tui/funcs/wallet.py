"""WAL: three kinds of wallet side by side (TERMINAL.md 10).

The Glimpse account (an API key can trade, never withdraw), a self-custodial Ark wallet through the user's own barkd,
and watch-only keys followed through the user's own node. The money path has the same manners as a Glimpse order:
the fee is shown first, the amount is typed back, a final confirmation is asked, and a send is never retried.
"""
from __future__ import annotations

import time
from typing import Any

from rich.text import Text

from .. import charts, fmt
from ..term import config, ui
from ..term.panes import FuncPane
from ..term.registry import Function, register
from ..theme import DIM, FAINT, GREEN, ORANGE, RED, TEXT
from ..wallet import barkd as B
from ..wallet import watch as W

EXPIRY_WARN_BLOCKS = 7 * 144            # a VTXO this close to expiry should be refreshed or exited


def qr_lines(data: str) -> list[Text]:
    """The payload as a QR code in half blocks. Lightning invoices go in upper case: a smaller, alphanumeric code."""
    import segno
    payload = data.upper() if data.lower().startswith(("lnbc", "lntb", "lnbcrt", "bc1", "tb1")) else data
    code = segno.make(payload, error="l", micro=False)
    return charts.qr([[bool(m) for m in row] for row in code.matrix], quiet=2)


class WalPane(FuncPane):
    code, every, tick = "WAL", 30, 5.0

    def __init__(self, hub, security=None, args=()) -> None:
        super().__init__(hub, security, args)
        self.bark: B.Barkd | None = None
        self.bal: B.Balance | None = None
        self.bark_error, self.network = "", ""
        self.vtxos: list[dict[str, Any]] = []
        self.tip = 0
        self.history: list[dict[str, Any]] = []
        self.boards: list[dict[str, Any]] = []
        self.exits: list[dict[str, Any]] = []
        self.watched: list[tuple[W.Watched | None, W.Scan | None, str]] = []
        self.receive: tuple[str, str, int] | None = None        # (kind, payload, amount) while the receive panel is open
        self.show = ""                                           # "" | history | exits | addresses
        self.busy = ""

    # loading ────────────────────────────────────────────────

    def _client(self) -> B.Barkd:
        url = self.hub.cfg.get("wallet", {}).get("barkd", "")
        if self.bark is None or self.bark.base_url != url.rstrip("/"):
            self.bark = B.Barkd(url)
        return self.bark

    async def load(self) -> None:
        self.title = "wallets"
        bark = self._client()
        if bark.configured:
            try:
                self.bal = await bark.balance()
                self.network = self.network or await bark.network()
                self.vtxos, self.tip = await bark.vtxos(), await bark.tip()
                self.boards, self.exits = await bark.boards(), await bark.exits()
                if self.show == "history":
                    self.history = await bark.history()
                self.bark_error = ""
            except B.BarkdError as e:
                self.bark_error = str(e)
        if not self.watched or time.time() - getattr(self, "_watched_at", 0) > 600:
            self._watched_at, out = time.time(), []
            for entry in self.hub.cfg.get("wallet", {}).get("watch", []):
                try:
                    w = W.parse(entry)
                    out.append((w, await W.scan(self.hub, w), ""))
                except W.WatchError as e:
                    out.append((locals().get("w") if isinstance(locals().get("w"), W.Watched) else None, None, str(e)))
            self.watched = out

    # drawing ────────────────────────────────────────────────

    def draw(self, w: int, h: int) -> list[Text]:
        price, out = self.hub.btc_price(), []
        out += self._glimpse(w, price) + [Text("")] + self._ark(w, price) + [Text("")] + self._watch(w, price)
        if self.receive:
            out += [Text("")] + self._receive(w, price)
        elif self.show == "history" and self.history:
            out += [Text(""), ui.section("ARK HISTORY", w)] + self._history(w)
        elif self.show == "exits":
            out += [Text(""), ui.section("BOARDS AND EXITS", w)] + self._exits(w)
        if self.busy:
            out += [Text(""), ui.note(self.busy, ORANGE)]
        return out

    def _glimpse(self, w: int, price: float | None) -> list[Text]:
        a = self.hub.account()
        head = ui.t(("GLIMPSE ACCOUNT  ", f"bold {ORANGE}"))
        if not a.get("authenticated"):
            return [ui.fit([head, ("read-only. ", DIM), ("L", f"bold {ORANGE}"),
                            (" logs in with an API key. A key can trade. It cannot withdraw.", DIM)], w)]
        wal, positions = a.get("wallet"), a.get("positions") or []
        lines = [ui.fit([head, (f"key {a.get('key_mask', '')}   ", DIM)] + ([ui.t(("balance ", FAINT), ui.sats_usd(wal.balance_sats, price)),
                         (f"   at risk {fmt.sats(wal.exposure_sats)} of {fmt.sats(wal.max_exposure_sats)}", DIM)] if wal else []), w)]
        value, cost = sum(p.value_sats for p in positions), sum(p.cost_sats for p in positions)
        pnl = value - cost
        lines.append(ui.fit([(f"  {len(positions)} open position{'s' if len(positions) != 1 else ''}", TEXT),
                             (f" · value {fmt.sats(value)} · P&L ", DIM), (fmt.sats(pnl, signed=True), GREEN if pnl >= 0 else RED),
                             ("      deposits and withdrawals: on the website (API keys cannot move funds)", FAINT)], w))
        return lines

    def _ark(self, w: int, price: float | None) -> list[Text]:
        url = self.hub.cfg.get("wallet", {}).get("barkd", "")
        head = ui.t(("ARK WALLET  ", f"bold {ORANGE}"))
        if not url:
            return [ui.fit([head, ("no daemon configured. ", DIM)], w),
                    *ui.wrap("barkd is Second's self-custodial Ark, Lightning and on-chain wallet. It holds the seed; the terminal only "
                             "calls its API. Run it, then: SET wallet.barkd http://127.0.0.1:3000 and press t here to store its token "
                             "in the keychain (`barkd secret show` prints it). Try it on signet first.", w, FAINT, "  ")]
        lines = [ui.fit([head, (f"barkd at {url.split('://')[-1]}", DIM),
                         (f"   {self.network}", f"bold {GREEN if self.network not in ('bitcoin', 'mainnet') else RED}")], w)]
        if self.bark_error:
            return lines + ui.wrap(self.bark_error, w, RED, "  ")
        if not self.bal:
            return lines + [ui.note("  loading…")]
        b = self.bal
        lines.append(ui.fit([("  spendable ", FAINT), ui.sats_usd(b.spendable, price), ("   pending ", FAINT), (fmt.sats(b.pending), TEXT),
                             ("   on-chain ", FAINT), (fmt.sats(b.onchain_total), TEXT)], w))
        soon = [v for v in self.vtxos if self.tip and int(v.get("expiry_height", 0)) - self.tip < EXPIRY_WARN_BLOCKS]
        nxt = min((int(v.get("expiry_height", 0)) for v in self.vtxos), default=0)
        tail: list = []
        if nxt and self.tip:
            days = (nxt - self.tip) / 144
            tail = [(f"   next VTXO expiry in {days:,.0f} days", RED if soon else FAINT)]
        lines.append(ui.fit([("  r", f"bold {ORANGE}"), (" receive  ", DIM), ("s", f"bold {ORANGE}"), (" send  ", DIM), ("b", f"bold {ORANGE}"),
                             (" board  ", DIM), ("x", f"bold {ORANGE}"), (" exits  ", DIM), ("h", f"bold {ORANGE}"), (" history  ", DIM),
                             ("t", f"bold {ORANGE}"), (" token", DIM), *tail], w))
        if soon:
            lines.append(ui.note(f"  {len(soon)} VTXO{'s' if len(soon) != 1 else ''} expire within a week. Refresh them in barkd, "
                                 "or they must be exited on chain.", RED))
        return lines

    def _watch(self, w: int, price: float | None) -> list[Text]:
        head = ui.t(("WATCH-ONLY  ", f"bold {ORANGE}"))
        if not self.hub.cfg.get("wallet", {}).get("watch"):
            return [ui.fit([head, ("nothing watched. ", DIM), ("a", f"bold {ORANGE}"),
                            (" adds an xpub, zpub or descriptor. Public keys only.", DIM)], w)]
        lines = []
        for wt, sc, err in self.watched:
            label = wt.label if wt else "entry"
            if err:
                lines += [ui.fit([head, (label, TEXT)], w)] + ui.wrap(err, w, RED if "private" in err else FAINT, "  ")
                continue
            if not sc:
                continue
            lines.append(ui.fit([head, (f"{label} · {wt.descriptor.split('(')[0]} · via {sc.backend}", DIM)], w))
            moved = f"last movement {ui.when(sc.last_seen)} ago" if sc.last_seen else "no movement seen"
            lines.append(ui.fit([("  ", ""), ui.sats_usd(sc.balance, price),
                                 (f"   {sc.utxos} UTXOs   {sc.used} used of {sc.scanned} scanned   {moved}", DIM),
                                 (f"   pending {fmt.sats(sc.pending, signed=True)}" if sc.pending else "", ORANGE)], w))
            if wt.note:
                lines.append(ui.note("  " + wt.note))
        return lines or [ui.fit([head, ("scanning…", FAINT)], w)]

    def _receive(self, w: int, price: float | None) -> list[Text]:
        kind, payload, amount = self.receive
        title = {"ark": "Ark address", "lightning": "Lightning invoice", "onchain": "on-chain address"}[kind]
        out = [ui.section(f"RECEIVE · {title}" + (f" · {fmt.sats(amount)}" if amount else ""), w)]
        try:
            code = qr_lines(payload)
        except Exception:
            code = []
        beside = bool(code) and code[0].cell_len + 24 <= w
        room = max(w - (code[0].cell_len + 3 if beside else 0), 16)
        text = [Text(payload[i:i + room], style=TEXT, no_wrap=True) for i in range(0, len(payload), room)]  # an invoice has no spaces to wrap at
        side = text + [Text(""), ui.t(("c", f"bold {ORANGE}"), (" copy   ", DIM), ("esc", f"bold {ORANGE}"), (" close", DIM))]
        if beside:
            for i in range(max(len(code), len(side))):
                left = code[i] if i < len(code) else Text(" " * code[0].cell_len)
                out.append(ui.t(left, "   ", side[i] if i < len(side) else ""))
        else:
            out += side + ([ui.note("Too narrow for the QR code. ctrl-w o zooms.")] if code else [])
        return out

    def _history(self, w: int) -> list[Text]:
        rows = []
        for m in self.history[:30]:
            t = str((m.get("time") or {}).get("created_at", ""))[:16].replace("T", " ")
            delta = int(m.get("effective_balance_sat", 0))
            sub = m.get("subsystem") or {}
            rows.append([t, f"{sub.get('name', '')} {sub.get('kind', '')}".strip(),
                         Text(fmt.sats(delta, signed=True), style=GREEN if delta >= 0 else RED),
                         fmt.sats(int(m.get("offchain_fee_sat", 0))), str(m.get("status", ""))])
        return ui.table([ui.Col("time UTC", "left"), ui.Col("what", "left", flex=True, style=DIM), ui.Col("amount"), ui.Col("fee", style=DIM),
                         ui.Col("status", "left", style=DIM)], rows, w)

    def _exits(self, w: int) -> list[Text]:
        out = [ui.note(f"{len(self.boards)} pending board{'s' if len(self.boards) != 1 else ''} · "
                       f"{len(self.exits)} exit{'s' if len(self.exits) != 1 else ''}")]
        for b in self.boards:
            out.append(ui.t(("board  ", FAINT), (fmt.sats(int(b.get("amount_sat", 0))), TEXT),
                            (f"  funding {str((b.get('funding_tx') or {}).get('txid', ''))[:16]}…", DIM)))
        for e in self.exits:
            state = (e.get("state") or {}).get("type", "") if isinstance(e.get("state"), dict) else str(e.get("state", ""))
            out.append(ui.t(("exit   ", FAINT), (str(e.get("vtxo_id", ""))[:20] + "…", TEXT),
                            (f"  {state}", ORANGE if state != "claimed" else DIM)))
        return out

    # keys ───────────────────────────────────────────────────

    def key(self, k: str, ch: str | None) -> bool:
        bark_ready = bool(self.bark and self.bark.configured and not self.bark_error)
        if k in ("escape", "backspace") and (self.receive or self.show):
            self.receive, self.show = None, ""
        elif ch == "c" and self.receive:
            self.app.copy_to_clipboard(self.receive[1])
            self.busy = "Copied."
        elif ch == "t":
            self.app.run_worker(self._set_token())
        elif ch == "a":
            self.app.run_worker(self._add_watch())
        elif ch == "r" and bark_ready:
            self.app.run_worker(self._receive_flow())
        elif ch == "s" and bark_ready:
            self.app.run_worker(self._send_flow())
        elif ch == "b" and bark_ready:
            self.app.run_worker(self._board_flow())
        elif ch in ("h", "x"):
            self.show = "" if self.show else {"h": "history", "x": "exits"}[ch]
            self.loaded_at = 0.0
        else:
            return False
        return True

    def hint(self) -> str:
        return "r receive · s send · b board · x exits · h history · a watch a key · t barkd token"

    async def _ask(self, title: str, hint: str, password: bool = False, placeholder: str = "") -> str:
        from ..app import Prompt
        return ((await self.app.push_screen_wait(Prompt(title, hint, password=password, placeholder=placeholder))) or "").strip()

    async def _set_token(self) -> None:
        v = await self._ask("barkd token",
                            "Paste the token `barkd secret show` prints. It goes to the OS keychain, never to a file or a log.",
                            password=True)
        if v:
            where = config.save_secret("barkd", v)
            self.bark, self.loaded_at, self.busy = None, 0.0, f"Token stored in the {where}."
            self.bump()

    async def _add_watch(self) -> None:
        v = await self._ask("Watch a key",
                            "An xpub, ypub, zpub or a descriptor such as wpkh(xpub…/<0;1>/*). Public keys only. Optional label: cold=zpub…")
        if not v:
            return
        try:
            W.parse(v)
        except W.WatchError as e:
            self.busy = str(e)
            self.bump()
            return
        self.hub.cfg.setdefault("wallet", {}).setdefault("watch", []).append(v)
        config.save(self.hub.cfg)
        self.watched, self.loaded_at, self.busy = [], 0.0, "Added. Scanning with a gap limit of 20."
        self.bump()

    async def _receive_flow(self) -> None:
        kind = (await self._ask("Receive", "a  an Ark address     l  a Lightning invoice     o  an on-chain address",
                                placeholder="a")).lower()[:1]
        try:
            if kind == "l":
                raw = await self._ask("Lightning invoice", "The amount in sats.", placeholder="50000")
                amount = int(raw.replace(",", "").replace("_", "") or 0)
                if amount <= 0:
                    return
                self.receive = ("lightning", await self.bark.invoice(amount, "Glimpse Terminal"), amount)
            elif kind == "o":
                self.receive = ("onchain", await self.bark.onchain_address(), 0)
            elif kind == "a":
                self.receive = ("ark", await self.bark.ark_address(), 0)
        except (B.BarkdError, ValueError) as e:
            self.busy = str(e)
        self.bump()

    async def _send_flow(self) -> None:
        """Destination, amount, the fee first, the amount typed back, then a final yes. One attempt, never retried."""
        from ..app import Confirm
        bark, cap = self.bark, int(self.hub.cfg.get("wallet", {}).get("send_cap_sats_per_day", 0))
        dest = await self._ask("Send · destination", "Paste a Lightning invoice, an Ark address or a Bitcoin address.")
        if not dest:
            return
        kind = B.kind_of(dest)
        asked = B.invoice_amount_sat(dest) if kind == "lightning" else None
        try:
            amount = asked if asked is not None else int((await self._ask("Send · amount",
                                                                          "The amount in sats.")).replace(",", "").replace("_", "") or 0)
        except ValueError:
            amount = 0
        try:
            bal = await bark.balance()
            fee = await bark.estimate(dest, amount) if kind and amount > 0 else B.Fee(0, amount, amount, known=False)
        except B.BarkdError as e:
            self.busy = str(e)
            self.bump()
            return
        if why := B.check_send(dest, amount, fee.fee_sat, bal.spendable, cap, self.network):
            self.busy = why
            self.bump()
            return
        price = self.hub.btc_price()
        fee_line = (f"fee {fmt.sats(fee.fee_sat)}" if fee.known
                    else "fee: barkd gives no estimate for this kind of send; the Ark server's schedule applies")
        typed = await self._ask("Send · type the amount again",
                                f"{fmt.sats(amount)} to {dest[:28]}… · {fee_line}. Type the amount in sats to go on.")
        if typed.replace(",", "").replace("_", "") != str(amount):
            self.busy = "The amounts did not match. Nothing was sent."
            self.bump()
            return
        body = Text.assemble(("to       ", FAINT), (f"{dest[:44]}{'…' if len(dest) > 44 else ''}\n", TEXT), ("kind     ", FAINT),
                             (f"{kind} · {B.network_of(dest)} · barkd on {self.network or 'unknown'}\n", TEXT), ("amount   ", FAINT),
                             ui.sats_usd(amount, price), ("\nfee      ", FAINT),
                             (f"{fmt.sats(fee.fee_sat) if fee.known else 'not estimated by barkd'}\n", TEXT),
                             ("cap      ", FAINT), (f"{fmt.sats(B.sent_today())} sent today of {fmt.sats(cap)}\n\n", DIM),
                             ("This cannot be undone. It is sent once and never retried.", f"bold {ORANGE}"))
        if not await self.app.push_screen_wait(Confirm(f"Send {fmt.sats(amount)}?", body)):
            self.busy = "Cancelled. Nothing was sent."
            self.bump()
            return
        try:
            res = await bark.send(dest, amount)
            B.record_sent(amount)
            self.busy = "Sent. " + str(res.get("message") or res.get("offboard_txid") or res.get("txid") or "")[:80]
        except B.BarkdError as e:
            self.busy = f"{e}. If barkd did not answer in time the payment may still have gone: press h for the history. It was not retried."
        self.loaded_at = 0.0
        self.bump()

    async def _board_flow(self) -> None:
        from ..app import Confirm
        raw = await self._ask("Board", "Move on-chain sats into the Ark. The amount in sats.")
        try:
            amount = int(raw.replace(",", "").replace("_", "") or 0)
        except ValueError:
            amount = 0
        if amount <= 0:
            return
        if not await self.app.push_screen_wait(Confirm(f"Board {fmt.sats(amount)}?",
                                                       Text("An on-chain transaction funds new VTXOs. It pays a miner fee.", style=DIM))):
            return
        try:
            await self.bark.board(amount)
            self.busy = "Board broadcast. It is spendable after the Ark server's confirmations."
        except B.BarkdError as e:
            self.busy = str(e)
        self.loaded_at = 0.0
        self.bump()


register(Function(
    "WAL", "Wallets", "Wallet", "the Glimpse account, your barkd Ark wallet, and watch-only keys", WalPane,
    needs=("glimpse account", "barkd", "address lookups"),
    help="Three wallets side by side. The Glimpse account shows the balance, what is at risk against its cap, and open positions "
         "valued locally. An API key can trade and cannot withdraw, so deposits and withdrawals stay on the website. "
         "The Ark wallet is your own barkd daemon (SET wallet.barkd http://127.0.0.1:3000, then t stores its token in the keychain). "
         "barkd alone holds the seed. The terminal never asks for it and never calls the routes that reveal or delete it. r makes an "
         "Ark address, a Lightning invoice or an on-chain address with a QR code. s sends: the fee estimate is shown first, the "
         "amount is typed back, a final confirmation is asked, the daily cap (wallet.send_cap_sats_per_day) is enforced, a "
         "destination on the wrong network is refused, and a send is attempted once and never retried. b boards on-chain sats "
         "into the Ark, x shows boards and exits, h the history, and VTXOs within a week of expiry raise a warning. "
         "Watch-only takes an xpub, ypub, zpub or descriptor (a adds one), derives addresses on this machine with a gap limit of "
         "20, and looks them up only on a backend on your own machines or through Tor, unless wallet.watch_public_ok is set. "
         "Refreshes every 30 s; watch-only rescans every 10 minutes. When barkd is down its section says so and the rest still works."))
