"""A client for barkd, Second's Bark daemon: a self-custodial Ark, Lightning and on-chain wallet the user runs.

barkd alone holds the seed. The terminal only calls its REST API (paths and shapes from barkd 0.7.1's OpenAPI,
recorded at tests/fixtures/sources/barkd/openapi.json) with a bearer token that lives in the keychain, never in a
file the terminal writes, a log, or an error message. Two routes exist that the terminal must never call and this
client does not define: the mnemonic, and wallet deletion.

The daemon is on localhost, so requests go direct: never through the SOCKS5 proxy the public sources use.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from ..term import config

API = "/api/v1"
NEVER = ("/wallet/mnemonic",)                           # and DELETE /wallet: not defined here, and refused in _call


class BarkdError(Exception):
    """Safe to show: never carries the token."""


@dataclass
class Balance:
    spendable: int = 0
    pending_in_round: int = 0
    pending_lightning_send: int = 0
    claimable_lightning_receive: int = 0
    pending_board: int = 0
    pending_exit: int = 0
    onchain_confirmed: int = 0
    onchain_pending: int = 0
    onchain_total: int = 0

    @property
    def pending(self) -> int:
        return (self.pending_in_round + self.pending_lightning_send + self.claimable_lightning_receive + self.pending_board
                + self.pending_exit)


@dataclass
class Fee:
    fee_sat: int
    gross_sat: int
    net_sat: int
    known: bool = True              # barkd has no estimate for an Ark-to-Ark send or a send from the on-chain wallet


def kind_of(destination: str) -> str:
    """'lightning', 'ark', 'onchain', 'lnaddress' or '' for a pasted destination."""
    d = destination.strip()
    low = d.lower().removeprefix("lightning:").removeprefix("bitcoin:")
    if re.match(r"^ln(bc|tb|tbs|bcrt)[0-9a-z]+$", low) or low.startswith("lno1"):
        return "lightning"
    if re.match(r"^t?ark1[0-9a-z]{20,}$", low):
        return "ark"
    if re.match(r"^(bc1|tb1|bcrt1)[0-9a-z]{11,87}$", low) or re.match(r"^[13mn2][1-9A-HJ-NP-Za-km-z]{25,39}$", d):
        return "onchain"
    if re.match(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", low):
        return "lnaddress"
    return ""


def invoice_amount_sat(invoice: str) -> int | None:
    """The amount a BOLT11 invoice asks for, from its human-readable part, or None for an amountless invoice."""
    m = re.match(r"^ln(?:bcrt|bc|tbs|tb)(\d+)([munp]?)1", invoice.strip().lower().removeprefix("lightning:"))
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    btc = n * {"": 1.0, "m": 1e-3, "u": 1e-6, "n": 1e-9, "p": 1e-12}[unit]
    return round(btc * 1e8)


def network_of(destination: str) -> str:
    low = destination.strip().lower()
    if low.startswith(("lnbcrt", "bcrt1")):
        return "regtest"
    if low.startswith(("lntbs", "lntb", "tb1", "tark1")) or destination[:1] in ("m", "n", "2"):
        return "signet/testnet"
    return "mainnet"


class Barkd:
    def __init__(self, base_url: str = "", token: str | None = None, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self._token = token if token is not None else (config.secret("barkd") or "")
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    @property
    def has_token(self) -> bool:
        return bool(self._token)

    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            kw: dict[str, Any] = {"timeout": httpx.Timeout(30.0, connect=4.0), "headers": {"Authorization": f"Bearer {self._token}"}}
            if self._transport is not None:
                kw["transport"] = self._transport
            self._client = httpx.AsyncClient(**kw)                  # direct: a localhost daemon is never reached through Tor
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def _call(self, method: str, path: str, params: dict | None = None, body: dict | None = None) -> Any:
        if not self.configured:
            raise BarkdError("barkd is not configured. SET wallet.barkd http://127.0.0.1:3000")
        if path in NEVER or (method == "DELETE" and path == "/wallet"):
            raise BarkdError("the terminal never asks barkd for the seed and never deletes a wallet")
        try:
            r = await self._http().request(method, self.base_url + API + path, params=params, json=body)
        except httpx.HTTPError as e:
            raise BarkdError(f"barkd did not answer at {self.base_url} ({type(e).__name__})") from None
        if r.status_code == 401:
            raise BarkdError("barkd refused the token. `barkd secret show` prints it; SET it again from the WAL page with t")
        if r.status_code >= 400:
            try:
                msg = r.json().get("message") or r.json().get("error") or r.text
            except (ValueError, AttributeError):
                msg = r.text
            raise BarkdError(f"barkd {r.status_code}: {str(msg)[:160]}")
        try:
            return r.json()
        except ValueError:
            return {}

    # reads ──────────────────────────────────────────────────

    async def ping(self) -> bool:
        try:
            await self._call("GET", "/ping")
            return True
        except BarkdError:
            return False

    async def network(self) -> str:
        info = await self._call("GET", "/wallet/ark-info")
        return str(info.get("network", "")) if isinstance(info, dict) else ""

    async def balance(self) -> Balance:
        w = await self._call("GET", "/wallet/balance")
        try:
            o = await self._call("GET", "/onchain/balance")
        except BarkdError:
            o = {}
        return Balance(int(w.get("spendable_sat", 0)), int(w.get("pending_in_round_sat", 0)), int(w.get("pending_lightning_send_sat", 0)),
                       int(w.get("claimable_lightning_receive_sat", 0)), int(w.get("pending_board_sat", 0)), int(w.get("pending_exit_sat") or 0),
                       int(o.get("confirmed_sat", 0)), int(o.get("trusted_pending_sat", 0)) + int(o.get("untrusted_pending_sat", 0)),
                       int(o.get("total_sat", 0)))

    async def vtxos(self) -> list[dict[str, Any]]:
        return list(await self._call("GET", "/wallet/vtxos"))

    async def tip(self) -> int:
        return int((await self._call("GET", "/bitcoin/tip")).get("tip_height", 0))

    async def history(self) -> list[dict[str, Any]]:
        return list(await self._call("GET", "/history"))

    async def boards(self) -> list[dict[str, Any]]:
        return list(await self._call("GET", "/boards/pending"))

    async def exits(self) -> list[dict[str, Any]]:
        return list(await self._call("GET", "/exits/status/all"))

    async def notifications(self, since: str = "") -> dict[str, Any]:
        return await self._call("GET", "/notifications/wait", {"since": since} if since else None)

    # receive ────────────────────────────────────────────────

    async def ark_address(self) -> str:
        return str((await self._call("POST", "/wallet/addresses/next")).get("address", ""))

    async def onchain_address(self) -> str:
        return str((await self._call("POST", "/onchain/addresses/next")).get("address", ""))

    async def invoice(self, amount_sat: int, description: str = "") -> str:
        body = {"amount_sat": int(amount_sat), **({"description": description} if description else {})}
        return str((await self._call("POST", "/lightning/receives/invoice", body=body)).get("invoice", ""))

    # fees and sends ─────────────────────────────────────────

    async def estimate(self, destination: str, amount_sat: int) -> Fee:
        """What barkd says the send will cost. It has an estimate for a Lightning payment and for an on-chain send
        from the Ark balance (an offboard). For an Ark-to-Ark send it has none (T12), and the terminal says so."""
        kind = kind_of(destination)
        if kind in ("lightning", "lnaddress"):
            v = await self._call("GET", "/fees/lightning/pay", {"amount_sat": amount_sat})
        elif kind == "onchain":
            v = await self._call("GET", "/fees/send-onchain", {"amount_sat": amount_sat, "address": destination})
        else:
            return Fee(0, amount_sat, amount_sat, known=False)
        return Fee(int(v.get("fee_sat", 0)), int(v.get("gross_amount_sat", amount_sat)), int(v.get("net_amount_sat", amount_sat)))

    async def send(self, destination: str, amount_sat: int, comment: str = "") -> dict[str, Any]:
        """One attempt, never retried: a timeout mid-send may still have paid (the same rule as Glimpse orders, PLAN D4)."""
        kind = kind_of(destination)
        if kind == "onchain":
            return await self._call("POST", "/wallet/send-onchain", body={"destination": destination, "amount_sat": int(amount_sat)})
        body: dict[str, Any] = {"destination": destination, "amount_sat": int(amount_sat)}
        if comment:
            body["comment"] = comment
        return await self._call("POST", "/wallet/send", body=body)

    async def board(self, amount_sat: int) -> dict[str, Any]:
        return await self._call("POST", "/boards/board-amount", body={"amount_sat": int(amount_sat)})


# ── the daily cap ───────────────────────────────────────────

def _ledger_path():
    return config.config_dir() / "wallet-sent.json"


def sent_today(now: float | None = None) -> int:
    day = datetime.fromtimestamp(now or time.time(), UTC).strftime("%Y-%m-%d")
    try:
        return int(json.loads(_ledger_path().read_text()).get(day, 0))
    except (OSError, ValueError):
        return 0


def record_sent(sats: int, now: float | None = None) -> None:
    day = datetime.fromtimestamp(now or time.time(), UTC).strftime("%Y-%m-%d")
    try:
        book = json.loads(_ledger_path().read_text())
    except (OSError, ValueError):
        book = {}
    book = {day: int(book.get(day, 0)) + int(sats)}                 # yesterday's total is no longer needed
    try:
        config.config_dir().mkdir(parents=True, exist_ok=True)
        _ledger_path().write_text(json.dumps(book))
    except OSError:
        pass


def check_send(destination: str, amount_sat: int, fee_sat: int, spendable: int, cap: int, daemon_network: str = "") -> str:
    """Why this send may not go ahead, or '' when it may. Every rule is checked before any confirmation is asked for."""
    kind = kind_of(destination)
    if not kind:
        return "That is not a Lightning invoice, an Ark address or a Bitcoin address."
    if amount_sat <= 0:
        return "The amount must be more than zero."
    if kind == "lightning" and (asked := invoice_amount_sat(destination)) is not None and asked != amount_sat:
        return f"The invoice asks for ₿{asked:,}, not ₿{amount_sat:,}."
    want = network_of(destination)
    if daemon_network and ((daemon_network in ("bitcoin", "mainnet")) != (want == "mainnet")):
        return f"The destination is a {want} one and barkd is on {daemon_network}."
    if amount_sat + fee_sat > spendable:
        return f"₿{amount_sat + fee_sat:,} with the fee is more than the ₿{spendable:,} that can be spent."
    left = cap - sent_today()
    if amount_sat > left:
        return f"The daily send cap is ₿{cap:,} and ₿{max(left, 0):,} of it is left today (SET wallet.send_cap_sats_per_day)."
    return ""
