"""Watch-only wallets: an xpub, ypub, zpub or output descriptor, followed without any key that can spend.

Addresses are derived on this machine with embit (MIT, pure Python) and looked up with a gap limit of 20 on both
the receive and the change branch. Looking an address up tells the backend you care about it, so derived addresses
go only to a backend on the user's own machines, or through Tor, unless `wallet.watch_public_ok` is set (rule 5).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..data.core import Provenance, SourceError

if TYPE_CHECKING:
    from ..term.hub import Hub

GAP = 20
KINDS = {"xpub": "pkh", "ypub": "sh-wpkh", "zpub": "wpkh", "tpub": "pkh", "upub": "sh-wpkh", "vpub": "wpkh"}


class WatchError(Exception):
    pass


@dataclass
class Watched:
    label: str
    source: str                         # what the user gave: a descriptor or an extended public key
    descriptor: str                     # the multipath descriptor actually derived from
    network: str                        # main | test
    note: str = ""


@dataclass
class Scan:
    balance: int = 0                    # confirmed, sats
    pending: int = 0                    # net mempool effect, sats
    utxos: int = 0
    used: int = 0                       # addresses with history
    scanned: int = 0
    last_seen: float = 0.0              # block time of the newest confirmed transaction
    backend: str = ""
    prov: Provenance | None = None
    rows: list[tuple[str, str, int, int]] = field(default_factory=list)     # (branch/index, address, balance, tx count)


def parse(entry: str | dict[str, Any]) -> Watched:
    """A config entry: a bare key or descriptor, `label=<key>`, or a table {label, key}. Private keys are refused."""
    from embit import bip32
    from embit.descriptor import Descriptor
    from embit.networks import NETWORKS

    if isinstance(entry, dict):
        label, src = str(entry.get("label", "")), str(entry.get("key", entry.get("descriptor", "")))
    else:
        label, _, src = str(entry).rpartition("=") if "=" in str(entry) and "(" not in str(entry).split("=")[0] else ("", "", str(entry))
    src = src.strip()
    if not src:
        raise WatchError("empty watch entry")
    if "prv" in src[:5].lower() or "prv" in src.lower().split("(")[-1][:5]:
        raise WatchError("that is a private key. The terminal only ever takes public keys and never holds a seed.")
    from ..term.instruments import ADDRESS
    if ADDRESS.match(src):                              # ADDR's `w` key watches one address: no derivation, one lookup
        return Watched(label or f"{src[:8]}…{src[-4:]}", src, f"addr({src})", "test" if src.startswith("tb1") else "main")
    try:
        if "(" in src:
            body = src.split("#")[0]
            if "/*" not in body:
                raise WatchError("a descriptor needs a wildcard, for example wpkh(xpub…/<0;1>/*)")
            desc = Descriptor.from_string(body)
            net = "test" if any(p in body for p in ("tpub", "upub", "vpub")) else "main"
            return Watched(label or body[:14] + "…", src, body, net)
        hd = bip32.HDKey.from_string(src)
        if hd.is_private:
            raise WatchError("that is a private key. The terminal only ever takes public keys and never holds a seed.")
        prefix = src[:4]
        kind = KINDS.get(prefix)
        if kind is None:
            raise WatchError(f"unknown key prefix {prefix}")
        net = "test" if prefix in ("tpub", "upub", "vpub") else "main"
        plain = hd.to_string(version=NETWORKS[net]["xpub"])
        body = {"pkh": f"pkh({plain}/<0;1>/*)", "sh-wpkh": f"sh(wpkh({plain}/<0;1>/*))", "wpkh": f"wpkh({plain}/<0;1>/*)"}[kind]
        desc = Descriptor.from_string(body)
        note = "An xpub is read as legacy (BIP44) addresses. Give a descriptor for anything else." if kind == "pkh" else ""
        assert desc is not None
        return Watched(label or f"{prefix}…{src[-4:]}", src, body, net, note)
    except WatchError:
        raise
    except Exception as e:
        raise WatchError(f"could not read that key or descriptor ({type(e).__name__})") from None


def addresses(w: Watched, branch: int, start: int, count: int) -> list[str]:
    from embit.descriptor import Descriptor
    from embit.networks import NETWORKS

    if w.descriptor.startswith("addr("):
        return [w.descriptor[5:-1]] if branch == 0 and start == 0 else []
    desc = Descriptor.from_string(w.descriptor)
    branch = min(branch, max(desc.num_branches - 1, 0))
    return [desc.derive(i, branch_index=branch).address(NETWORKS[w.network]) for i in range(start, start + count)]


def backend_for(hub: Hub) -> tuple[str, str]:
    """(source key, '') for the backend a watch-only scan may use, or ('', why not)."""
    allow_public = bool(hub.cfg.get("wallet", {}).get("watch_public_ok"))
    for key in hub.bitcoin_order():
        if not hub.is_public(key):
            return key, ""
    if allow_public and hub.bitcoin_order():
        return hub.bitcoin_order()[0], ""
    return "", ("Derived addresses never go to a public backend without consent. Point a backend at your own node "
                "(SET bitcoin.mempool http://your-node:3006), route through Tor (SET socks5 socks5h://127.0.0.1:9050), "
                "or accept the leak with SET wallet.watch_public_ok true.")


async def scan(hub: Hub, w: Watched, max_addresses: int = 200) -> Scan:
    key, why = backend_for(hub)
    if not key:
        raise WatchError(why)
    src, out = hub.sources[key], Scan(backend=hub.sources[key].host)
    for branch in (0, 1):
        gap, i = 0, 0
        while gap < GAP and i < max_addresses:
            batch = await asyncio.to_thread(addresses, w, branch, i, GAP)       # derivation is pure Python: keep it off the loop
            for j, a in enumerate(batch):
                try:
                    info, prov = await src.address(a)
                except SourceError as e:
                    raise WatchError(str(e)) from None
                out.prov, out.scanned = prov, out.scanned + 1
                c, m = info.get("chain_stats", {}), info.get("mempool_stats", {})
                txs = int(c.get("tx_count", 0)) + int(m.get("tx_count", 0))
                if txs == 0:
                    gap += 1
                    if gap >= GAP:
                        break
                    continue
                gap = 0
                bal = int(c.get("funded_txo_sum", 0)) - int(c.get("spent_txo_sum", 0))
                out.balance += bal
                out.pending += int(m.get("funded_txo_sum", 0)) - int(m.get("spent_txo_sum", 0))
                out.utxos += int(c.get("funded_txo_count", 0)) - int(c.get("spent_txo_count", 0))
                out.used += 1
                out.rows.append((f"{branch}/{i + j}", a, bal, txs))
                try:                                                # the last movement: the newest confirmed transaction's block time
                    hist, _ = await src.address_txs(a)
                    out.last_seen = max([out.last_seen] + [float(t.get("status", {}).get("block_time", 0) or 0) for t in hist])
                except (SourceError, AttributeError):
                    pass
            i += GAP
    return out
