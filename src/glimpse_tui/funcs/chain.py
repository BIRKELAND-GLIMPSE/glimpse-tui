"""BLK, TX, ADDR and RBF: the chain explorer. Any block, any transaction explained, any address, replacements live.

Every lookup walks `hub.bitcoin_order()` through `ask`, so a self-hosted mempool, Bitview or Esplora answers when the
first backend cannot, and the frame names the one that did. A transaction or address lookup on a public backend
tells its operator what you care about, so the first one says so (TERMINAL.md rule 5) and points at `SET`.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from rich.cells import cell_len
from rich.text import Text

from .. import charts, fmt
from ..data import btcmath
from ..data.core import Provenance, SourceError
from ..term import config, ui
from ..term.panes import FuncPane
from ..term.registry import Function, register
from ..theme import DIM, FAINT, GREEN, ORANGE, RED, TEXT
from .btc import bands_picture, fee_span, fee_text

PAGE = 25                       # transactions a page of a block, fixed by the API
MAX_ROWS = 100                  # inputs or outputs drawn; EXP still exports every one
RBF_SEQUENCE = 0xFFFFFFFE       # any input sequence below this signals replaceability (BIP 125)
DASH = "–"


# ── asking the first backend that can answer ────────────────

async def ask(hub, method: str, *args: Any, skip: set[str] | None = None, via: list[str] | None = None) -> tuple[Any, Provenance]:
    """Call `method` on the first Bitcoin backend in the user's order that has it and answers.

    A backend without the method is passed over (Esplora has no CPFP). One that fails is passed over too, and when
    `skip` is given a host that could not be reached is remembered there, so the rest of a page load does not wait
    out its timeouts again. An HTTP error is an answer about one request, and the host is asked again.
    `via` collects the key of the backend that answered."""
    last: SourceError | None = None
    for key in hub.bitcoin_order():
        if skip is not None and key in skip:
            continue
        try:
            value, prov = await getattr(hub.sources[key], method)(*args)
        except AttributeError:
            continue
        except SourceError as e:
            last = e
            if skip is not None and "HTTP " not in str(e):       # no status at all: the host is unreachable or resting
                skip.add(key)
            continue
        if via is not None:
            via.append(key)
        return value, prov
    raise last or SourceError(f"no Bitcoin backend answers {method}")


def settled(prov: Provenance, as_of: float) -> Provenance:
    """Provenance for something that can no longer change: a confirmed block or transaction. The cache keeps these
    for good, so their fetch time says nothing about freshness; the value is as of its block, checked at this load."""
    return replace(prov, fetched_at=time.time(), as_of=as_of or prov.as_of)


# ── small formatters ────────────────────────────────────────

def mid(s: str, width: int) -> str:
    """Shorten in the middle, where an address or a hash carries the least: `bc1qwqdg…wvvzej`."""
    if len(s) <= width or width < 5:
        return s if len(s) <= width else s[:max(width, 0)]
    head = width // 2
    return s[:head] + "…" + s[len(s) - (width - 1 - head):]


def utc(ts: float, seconds: bool = True) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%d %b %Y %H:%M:%S UTC" if seconds else "%d %b %H:%M")


def clock(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%H:%M:%S")


def sat(n: float) -> Text:
    return Text(fmt.sats(n), style=f"bold {ORANGE}", no_wrap=True)


def rate_text(rate: float | None, suffix: str = "") -> Text:
    if rate is None:
        return Text(DASH, style=FAINT)
    return Text(fee_text(rate) + suffix, style=f"bold {charts.snap(charts.fee_colour(max(rate, 1.0)))}", no_wrap=True)


def rate_words(rate: float) -> str:
    """A transaction's own fee rate, with the decimals a bump of 0.2 sat/vB needs."""
    return f"{rate:,.0f}" if rate >= 1000 else f"{rate:,.1f}" if rate >= 10 else f"{rate:.2f}"


def exact_rate(rate: float | None, suffix: str = "") -> Text:
    if rate is None:
        return rate_text(None)
    return Text(rate_words(rate) + suffix, style=f"bold {charts.snap(charts.fee_colour(max(rate, 1.0)))}", no_wrap=True)


def btc3(sats: float | None) -> str:
    return DASH if sats is None else f"{sats / 1e8:,.3f}"


def script_type(kind: str) -> str:
    return (kind or "?").removeprefix("v0_").removeprefix("v1_")


def vsize_of(tx: dict) -> float:
    return float(tx.get("weight", 0)) / 4


def rate_of(tx: dict) -> float | None:
    vs = vsize_of(tx)
    return float(tx.get("fee", 0)) / vs if vs and not is_coinbase(tx) else None


def is_coinbase(tx: dict) -> bool:
    return any(i.get("is_coinbase") for i in tx.get("vin", []))


def signals_rbf(tx: dict) -> bool:
    return any(int(i.get("sequence", 0xFFFFFFFF)) < RBF_SEQUENCE for i in tx.get("vin", []))


def printable(s: str) -> bool:
    return bool(s) and all(ch.isprintable() and cell_len(ch) == 1 for ch in s)


def op_return_payload(script_hex: str) -> bytes:
    """The data pushes after OP_RETURN, joined."""
    try:
        raw = bytes.fromhex(script_hex)
    except ValueError:
        return b""
    out, i = b"", 1
    while i < len(raw):
        op, i = raw[i], i + 1
        if op <= 0x4B:
            n = op
        elif op in (0x4C, 0x4D, 0x4E):
            size = {0x4C: 1, 0x4D: 2, 0x4E: 4}[op]
            n, i = int.from_bytes(raw[i:i + size], "little"), i + size
        else:
            continue                                            # OP_1..OP_16 and the like carry no bytes
        out, i = out + raw[i:i + n], i + n
    return out


def op_return_text(script_hex: str) -> tuple[str, bool]:
    """(payload, is_text): text when the bytes are printable UTF-8 of one-cell glyphs, else hex."""
    data = op_return_payload(script_hex)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = ""
    return (text, True) if printable(text) else (data.hex(), False)


def coinbase_tag(ascii_sig: str) -> str:
    """The readable part of a coinbase script: runs of four or more printable ASCII characters."""
    runs, run = [], ""
    for ch in ascii_sig or "":
        if " " <= ch <= "~":
            run += ch
        else:
            runs, run = runs + ([run.strip()] if len(run.strip()) >= 4 else []), ""
    runs += [run.strip()] if len(run.strip()) >= 4 else []
    return " ".join(runs)


def chunks(s: str, width: int, limit: int = 6) -> list[str]:
    width = max(width, 8)
    parts = [s[i:i + width] for i in range(0, len(s), width)]
    return parts[:limit - 1] + [mid("".join(parts[limit - 1:]), width)] if len(parts) > limit else parts


def flow(parts: list[Text], w: int, hang: int = 0, sep: str = "   ") -> list[Text]:
    """Pack label and value pairs onto as few lines as fit: one line at 196 columns, several at 44. Lines after the
    first start `hang` cells in, under the first value."""
    out: list[Text] = []
    line, used = Text(no_wrap=True), 0
    for p in parts:
        if used and line.cell_len + len(sep) + p.cell_len > w:
            out.append(line)
            line, used = Text(" " * (hang if hang + p.cell_len <= w else 0), no_wrap=True), 0
        if used:
            line.append(sep)
        line.append_text(p)
        used += 1
    return out + ([line] if used else [])


def sect(title: str, w: int, right: str = "") -> Text:
    """A section head whose right-hand note is given up when the pane is too narrow for both."""
    return ui.section(title, w, right if len(title) + len(right) + 5 <= w else "")


def fact(label: str, value: str | Text, w: int, label_w: int = 12) -> Text:
    """One `label value` row; a plain string too long for the row is shortened in the middle."""
    if isinstance(value, str):
        value = mid(value, max(w - label_w - 1, 8))
    return ui.kv(f"{label:<{label_w}}", value, gap=1)


@dataclass
class C:
    """A table column with the order it is given up in when the pane is narrow (highest `drop` goes first).
    An `elastic` column holds plain strings and takes the spare width, shortening them in the middle."""
    col: ui.Col
    drop: int = 0
    elastic: int = 0


def fit(specs: list[C], rows: list[list[Any]], w: int, cursor: int | None = None) -> list[Text]:
    def cells(v: Any) -> int:
        return v.cell_len if isinstance(v, Text) else len(str(v if v is not None else ""))

    def natural(i: int) -> int:
        return specs[i].col.width or max([len(specs[i].col.head)] + [cells(r[i]) for r in rows])

    keep = [i for i in range(len(specs)) if not rows or any(cells(r[i]) for r in rows)]         # an empty column is not drawn
    width = {i: natural(i) for i in keep}

    def need(idx: list[int]) -> int:
        return sum(min(width[i], specs[i].elastic) if specs[i].elastic else width[i] for i in idx) + 2 * (len(idx) - 1)

    while len(keep) > 2 and need(keep) > w:
        keep.remove(max(keep, key=lambda i: specs[i].drop))
    cols = [specs[i].col for i in keep]
    body = [[r[i] for i in keep] for r in rows]
    for at, i in enumerate(keep):
        if specs[i].elastic:
            room = max(min(width[i], w - need([k for k in keep if k != i]) - 2), specs[i].elastic)
            body = [r[:at] + [mid(str(r[at] or ""), room)] + r[at + 1:] for r in body]
    return ui.table(cols, body, w, cursor)


def confirmations(tip: int, height: int | None) -> int:
    return max(tip - height + 1, 1) if tip and height else 0


# ── the base: backends, privacy, the row cursor ─────────────

class ChainPane(FuncPane):
    every = 60

    def __init__(self, hub, security=None, args=()) -> None:
        super().__init__(hub, security, args)
        self.down: set[str] = set()
        self.notice_host = ""                   # set when this lookup went to a public backend before the user was told
        self.tip = 0
        self.moved = False
        self.line_of: dict[int, int] = {}

    async def _ask(self, method: str, *args: Any, provs: list[Provenance] | None = None, private: bool = False) -> Any:
        via: list[str] = []
        value, prov = await ask(self.hub, method, *args, skip=self.down, via=via)
        if provs is not None and prov.source not in [p.source for p in provs]:
            provs.append(prov)
        if private and via and self.hub.is_public(via[0]) and not self.hub.cfg.get("privacy_ack"):
            self.notice_host = self.hub.sources[via[0]].host
        return value

    async def _try(self, method: str, *args: Any, provs: list[Provenance] | None = None) -> Any:
        try:
            return await self._ask(method, *args, provs=provs)
        except SourceError:
            return None

    async def _tip(self) -> None:
        """Confirmations need the tip. The stream keeps `hub.chain.height`; without it, ask once a load."""
        if not self.hub.chain.height:
            self.tip = await self._try("tip_height") or self.tip

    def height_now(self) -> int:
        return max(self.hub.chain.height, self.tip)

    def start_load(self) -> None:
        self.down = set()
        if self.hub.cfg.get("privacy_ack"):
            self.notice_host = ""

    def privacy(self, w: int) -> list[Text]:
        """Rule 5, once: the first lookup that left the machine says where it went. Drawing it records that it was shown."""
        if not self.notice_host:
            return []
        if not self.hub.cfg.get("privacy_ack"):
            self.hub.cfg["privacy_ack"] = True
            config.save(self.hub.cfg)
        text = f"This lookup goes to {self.notice_host}. Use your own node or Tor to keep it private (SET)."
        return ui.wrap(text, w, style=f"bold {ui.YELLOW}") + [Text("")]

    def cursor_in(self, first: int, count: int) -> int | None:
        return self.cur - first if first <= self.cur < first + count else None

    # A page opens at its top: the facts first, the tables below. The view starts following the cursor once it moves.

    def on_key_(self, k: str, ch: str | None, n: int = 1) -> bool:
        if k in ("j", "k", "down", "up", "ctrl+d", "ctrl+u", "ctrl+f", "ctrl+b", "pagedown", "pageup") or ch == "G":
            self.moved = True
        return super().on_key_(k, ch, n)

    def home(self) -> None:
        self.moved = False
        super().home()

    def keep_in_view(self, row: int, h: int) -> None:
        if self.moved:
            self.follow(row, h)


# ── BLK ─────────────────────────────────────────────────────

FEE_BANDS = ("min", "p10", "p25", "median", "p75", "p90", "max")
FEE_BANDS_SHORT = ("min", "p10", "p25", "med", "p75", "p90", "max")


class BlkPane(ChainPane):
    code, selectable = "BLK", True

    def __init__(self, hub, security=None, args=()) -> None:
        super().__init__(hub, security, args)
        self.arg = self.args[0].replace(",", "").lower() if self.args else ""
        self.blocks: dict[int, dict] = {}       # list mode: height -> block, grown a page at a time
        self.want_older = False
        self.block: dict = {}
        self.txs: list[dict] = []
        self.page = 0
        self.title = "recent blocks" if not self.arg else "block"

    # loading ────────────────────────────────────────────────

    async def load(self) -> None:
        self.start_load()
        await (self._load_block() if self.arg else self._load_list())

    async def _load_list(self) -> None:
        provs: list[Provenance] = []
        under = self._rows()[self.cur]["height"] if self.blocks and self.cur < len(self.blocks) else None
        try:
            latest = await self._ask("blocks", None, provs=provs)
        except SourceError:
            if not self.blocks:
                raise
            latest = []
        older = []
        if self.want_older and self.blocks and min(self.blocks) > 0:
            older = await self._try("blocks", min(self.blocks) - 1, provs=provs) or []
        self.want_older = False
        for b in list(latest) + list(older):
            self.blocks[int(b["height"])] = b
        for h in sorted(self.blocks)[:-300]:                    # a long session keeps the newest 300
            del self.blocks[h]
        rows = self._rows()
        if older:
            under = int(older[0]["height"])                     # land on the first block of the new page
        if under is not None:
            self.cur = next((i for i, b in enumerate(rows) if b["height"] == under), self.cur)
        self.title, self.provs = "recent blocks", provs or self.provs

    async def _load_block(self) -> None:
        provs: list[Provenance] = []
        block_hash = self.arg
        if self.arg.isdigit():
            block_hash = await self._ask("block_hash", int(self.arg), provs=provs)
        elif len(self.arg) != 64:
            raise SourceError(f"BLK takes a height or a 64-character block hash, not {self.arg[:20]}")
        self.block = await self._ask("block", block_hash, provs=provs)
        txs = await self._try("block_txs", block_hash, self.page * PAGE, provs=provs)
        if txs is not None:
            self.txs = txs
        await self._tip()
        self.title = f"block {int(self.block.get('height', 0)):,}"
        self.provs = [settled(p, float(self.block.get("timestamp", 0))) for p in provs]

    def _rows(self) -> list[dict]:
        return [self.blocks[h] for h in sorted(self.blocks, reverse=True)]

    # keys ───────────────────────────────────────────────────

    def _older(self) -> None:
        if not self.want_older:
            self.want_older, self.loaded_at = True, 0.0         # the shell reloads a pane whose load time is zero

    def on_key_(self, k: str, ch: str | None, n: int = 1) -> bool:
        if not self.arg and k in ("j", "down") and self.n_rows and self.cur >= self.n_rows - 1:
            self._older()                                       # scrolling past the last row pages to older blocks
            self.bump()
            return True
        return super().on_key_(k, ch, n)

    def key(self, k: str, ch: str | None) -> bool:
        if not self.arg:
            self.moved = True
            if ch == "n":
                self.cur = max(self.n_rows - 1, 0)
                self._older()
            elif ch == "p":
                self.cur = max(self.cur - 15, 0)
            else:
                return False
            return True
        pages = max((int(self.block.get("tx_count", 0)) - 1) // PAGE, 0)
        if ch == "]" and self.page < pages:
            self.page += 1
        elif ch == "[" and self.page > 0:
            self.page -= 1
        else:
            return False
        self.cur, self.txs, self.loaded_at = 0, [], 0.0
        return True

    def enter(self) -> str | None:
        if self.arg:
            return f"TX {self.txs[self.cur]['txid']}" if self.cur < len(self.txs) else None
        rows = self._rows()
        return f"BLK {rows[self.cur]['id']}" if self.cur < len(rows) else None

    def hint(self) -> str:
        return "enter opens the tx · ] [ page" if self.arg else "enter opens the block · n older · p back"

    def menu(self) -> list[tuple[str, str]]:
        if self.arg and self.block:
            h = int(self.block.get("height", 0))
            return [("previous", f"BLK {max(h - 1, 0)}"), ("next", f"BLK {h + 1}"), ("BLK", "BLK"), ("MEMP", "MEMP"), ("MINE", "MINE")]
        return [("MEMP", "MEMP"), ("FEES", "FEES"), ("RBF", "RBF"), ("MINE", "MINE")]

    def export(self):
        if self.arg:
            return (["index", "txid", "inputs", "outputs", "vsize", "fee_sat", "sat_per_vb", "out_value_sat"],
                    [[self.page * PAGE + i, t["txid"], len(t.get("vin", [])), len(t.get("vout", [])), vsize_of(t), t.get("fee"), rate_of(t),
                      sum(o.get("value", 0) for o in t.get("vout", []))] for i, t in enumerate(self.txs)])
        rows = []
        for b in self._rows():
            x = b.get("extras") or {}
            rows.append([b["height"], b["id"], b.get("timestamp"), (x.get("pool") or {}).get("name"), b.get("tx_count"), b.get("size"),
                         b.get("weight"), x.get("totalFees"), x.get("medianFee"), x.get("reward"), x.get("matchRate")])
        return ["height", "hash", "time", "pool", "txs", "size", "weight", "total_fees_sat", "median_fee", "reward_sat",
                "audit_health_pct"], rows

    # drawing ────────────────────────────────────────────────

    def draw(self, w: int, h: int) -> list[Text]:
        return self._draw_block(w, h) if self.arg else self._draw_list(w, h)

    def _draw_list(self, w: int, h: int) -> list[Text]:
        blocks, now = self._rows(), time.time()
        self.n_rows = len(blocks)
        if not blocks:
            return []
        rows = []
        for b in blocks:
            x = b.get("extras") or {}
            med, health = x.get("medianFee"), x.get("matchRate")
            rows.append([f"{int(b['height']):,}", ui.when(float(b.get("timestamp", now)), now), (x.get("pool") or {}).get("name") or DASH,
                         f"{int(b.get('tx_count', 0)):,}", rate_text(float(med)) if med is not None else None,
                         btc3(x.get("totalFees")), btc3(x.get("reward")), f"{b.get('size', 0) / 1e6:.2f}",
                         f"{health:.4g}%" if health is not None else DASH, f"{b.get('weight', 0) / 1e6:.2f}",
                         fee_span(x.get("feeRange") or []), utc(float(b.get("timestamp", now)), False)])
        specs = [C(ui.Col("height", style=f"bold {ORANGE}")), C(ui.Col("age", style=DIM), 1), C(ui.Col("pool", "left"), 2, elastic=8),
                 C(ui.Col("txs"), 3), C(ui.Col("med s/vB"), 4), C(ui.Col("fees ₿"), 5), C(ui.Col("reward ₿"), 6), C(ui.Col("MB", style=DIM), 7),
                 C(ui.Col("health", style=DIM), 8), C(ui.Col("MWU", style=DIM), 9), C(ui.Col("fee range", style=DIM), 10),
                 C(ui.Col("mined UTC", style=FAINT), 11)]
        out = [sect("RECENT BLOCKS", w, f"{len(blocks)} blocks · newest first")]
        out += fit(specs, rows, w, self.cur)
        if self.want_older:
            out.append(ui.note("loading older blocks…"))
        if not any(b.get("extras") for b in blocks):
            out.append(ui.note("This backend serves no pool, fee or audit fields."))
        self.follow(self.cur + 3, h)
        return out

    def _draw_block(self, w: int, h: int) -> list[Text]:
        b, now = self.block, time.time()
        if not b:
            return []
        x = b.get("extras") or {}
        ts, height = float(b.get("timestamp", 0)), int(b.get("height", 0))
        pool = (x.get("pool") or {}).get("name") or ""
        conf = confirmations(self.height_now(), height)
        lead = [Text(f"#{height:,}", style=f"bold {ORANGE}"), Text(pool, style=f"bold {TEXT}"),
                Text(f"{int(b.get('tx_count', 0)):,} tx", style=DIM), Text(f"{ui.when(ts, now)} ago", style=DIM)]
        if conf:
            lead.append(Text(f"{conf:,} confirmation{'s' if conf != 1 else ''}", style=DIM))
        out = flow(lead, w)
        out += [fact("hash", str(b.get("id", "")), w), fact("mined", utc(ts) if w >= 38 else utc(ts, False), w)]
        out += flow([ui.kv(f"{'size':<12}", f"{b.get('size', 0) / 1e6:.2f} MB"), ui.kv("weight", f"{b.get('weight', 0) / 1e6:.2f} MWU"),
                     *([ui.kv("virtual", f"{float(x['virtualSize']):,.0f} vB")] if x.get("virtualSize") else [])], w, 13)
        out += flow([ui.kv(f"{'version':<12}", f"0x{int(b.get('version', 0)):08x}"), ui.kv("bits", f"0x{int(b.get('bits', 0)):08x}"),
                     ui.kv("nonce", f"{int(b.get('nonce', 0)):,}")], w, 13)
        diff = float(b.get("difficulty", 0))
        out += [fact("difficulty", f"{diff / 1e12:,.2f} T" + (f"  ({diff:,.0f})" if w >= 60 else ""), w),
                fact("merkle root", str(b.get("merkle_root", "")), w), fact("previous", str(b.get("previousblockhash", "")), w)]
        if tag := coinbase_tag(x.get("coinbaseSignatureAscii", "")):
            out.append(fact("coinbase tag", tag, w))
        if x.get("coinbaseAddress"):
            out.append(fact("pays to", str(x["coinbaseAddress"]), w))
        if rng := x.get("feeRange"):
            out += [Text(""), sect("FEE RATES", w, "sat/vB by percentile")] + self._fee_strip(rng, w)
            if h >= 30:
                out += bands_picture(rng, float(x.get("virtualSize") or b.get("weight", 0) / 4) / 1e6, w, 2)
        if x.get("reward") is not None:
            out += [Text("")] + self._reward(x, height, w)
        if x.get("matchRate") is not None:
            out += [Text(""), sect("AUDIT", w, "the block against the expected template")]
            audit = [ui.kv("health", f"{x['matchRate']:.4g}%")]
            if x.get("expectedFees"):
                audit.append(ui.kv("expected fees", sat(x["expectedFees"])))
            if x.get("expectedWeight"):
                audit.append(ui.kv("expected weight", f"{x['expectedWeight'] / 1e6:.2f} MWU"))
            if x.get("similarity") is not None:
                audit.append(ui.kv("similarity", f"{x['similarity']:.3f}"))
            out += flow(audit, w)
        out += [Text("")] + self._tx_table(w, len(out) + 1, h)
        return out

    def _fee_strip(self, rng: list[float], w: int) -> list[Text]:
        cw = max(min(w // max(len(rng), 1), 12), 5)
        names = (FEE_BANDS if cw > 7 else FEE_BANDS_SHORT) if len(rng) == len(FEE_BANDS) else tuple(f"{i}" for i in range(len(rng)))
        labels, values, bar = Text(no_wrap=True), Text(no_wrap=True), Text(no_wrap=True)
        for name, r in zip(names, rng, strict=True):
            colour = charts.snap(charts.fee_colour(max(float(r), 1.0)))
            labels.append(f"{name[:cw - 1]:>{cw}}", style=FAINT)
            values.append(f"{fee_text(float(r)):>{cw}}", style=f"bold {colour}")
            bar.append(" " + "▀" * (cw - 1), style=colour)
        return [labels, values, bar]

    def _reward(self, x: dict, height: int, w: int) -> list[Text]:
        price = self.hub.btc_price()
        reward, fees = float(x.get("reward", 0)), float(x.get("totalFees", 0))
        subsidy = reward - fees if reward else float(btcmath.subsidy_sats(height))
        share = fees / reward if reward else 0.0
        out = [sect("REWARD", w, f"fees are {share:.2%} of it")]
        out += flow([ui.kv("subsidy", ui.sats_usd(subsidy, price)), ui.kv("fees", ui.sats_usd(fees, price)),
                     ui.kv("total", ui.sats_usd(reward, price))], w)
        bar_w = max(w - 22, 8)
        filled = charts.hbar(1 - share, bar_w)
        out.append(ui.t(("subsidy ", FAINT), (filled, ORANGE), ("█" * max(bar_w - len(filled), 1 if fees else 0), ui.CYAN), (" fees", FAINT)))
        if x.get("avgFee") is not None:
            more = [ui.kv("average fee", sat(x["avgFee"])), ui.kv("average rate", rate_text(float(x.get("avgFeeRate", 0)), " sat/vB"))]
            if x.get("medianFee") is not None:
                more.append(ui.kv("median", rate_text(float(x["medianFee"]), " sat/vB")))
            if x.get("totalInputs"):
                more += [ui.kv("inputs", f"{x['totalInputs']:,}"), ui.kv("outputs", f"{x.get('totalOutputs', 0):,}")]
            out += flow(more, w)
        return out

    def _tx_table(self, w: int, first_line: int, h: int) -> list[Text]:
        total, start = int(self.block.get("tx_count", 0)), self.page * PAGE
        self.n_rows = len(self.txs)
        head = sect("TRANSACTIONS", w, f"{start + 1:,}-{min(start + (len(self.txs) or PAGE), total):,} of {total:,}")
        if not self.txs:
            waiting = self.fetching or not self.loaded_at
            return [head, ui.note("loading this page…" if waiting else "This backend did not return the transactions.")]
        rows = []
        for i, t in enumerate(self.txs):
            cb = is_coinbase(t)
            rows.append([f"{start + i:,}", t["txid"], "coinbase" if cb else f"{len(t.get('vin', [])):,}", f"{len(t.get('vout', [])):,}",
                         f"{vsize_of(t):,.0f}", None if cb else sat(t.get("fee", 0)), rate_text(rate_of(t)),
                         sat(sum(o.get("value", 0) for o in t.get("vout", [])))])
        specs = [C(ui.Col("#", style=FAINT), 6), C(ui.Col("txid", "left"), 0, elastic=13), C(ui.Col("in", style=DIM), 4),
                 C(ui.Col("out", style=DIM), 5), C(ui.Col("vB", style=DIM), 3), C(ui.Col("fee"), 2), C(ui.Col("s/vB"), 0),
                 C(ui.Col("value out"), 1)]
        self.keep_in_view(first_line + 3 + self.cur, h)
        return [head] + fit(specs, rows, w, self.cur)


# ── TX ──────────────────────────────────────────────────────

def walk_tree(node: dict, newer: dict | None = None, prefix: str = "", lead: str = "") -> list[tuple[str, dict, dict | None]]:
    """A replacement tree flattened for drawing, newest first: (tree prefix, node, the version that replaced it).
    A chain of single replacements stays in one column; only a version that replaced several at once branches."""
    out = [(lead, node, newer)]
    kids = node.get("replaces") or []
    for i, kid in enumerate(kids):
        last = i == len(kids) - 1
        if len(kids) == 1:
            out += walk_tree(kid, node, prefix, prefix + "└─ ")
        else:
            out += walk_tree(kid, node, prefix + ("   " if last else "│  "), prefix + ("└─ " if last else "├─ "))
    return out


def fee_bump(old: dict, new: dict) -> tuple[float, float, float]:
    """(sat/vB added, fraction added, sats added) going from one version's `tx` to the next."""
    a, b = float(old.get("rate", 0)), float(new.get("rate", 0))
    return b - a, (b / a - 1) if a else 0.0, float(new.get("fee", 0)) - float(old.get("fee", 0))


def gap_text(seconds: float) -> str:
    s = int(max(seconds, 0))
    return f"{s}s" if s < 600 else f"{s // 60}m" if s < 7200 else f"{s // 3600}h"


class TxPane(ChainPane):
    code, selectable = "TX", True

    def __init__(self, hub, security=None, args=()) -> None:
        super().__init__(hub, security, args)
        self.txid = self.args[0].lower() if self.args else ""
        self.tx: dict = {}
        self.spends: list[dict] = []
        self.cpfp: dict = {}
        self.rbf: dict = {}
        self.first_seen = 0.0
        self.mblocks: list[dict] = []
        self.title = f"tx {mid(self.txid, 17)}" if self.txid else "transaction"

    async def load(self) -> None:
        self.start_load()
        if len(self.txid) != 64 or any(ch not in "0123456789abcdef" for ch in self.txid):
            raise SourceError("TX takes a 64-character transaction id: TX <txid>")
        provs: list[Provenance] = []
        try:
            self.tx = await self._ask("tx", self.txid, provs=provs, private=True)
        except SourceError as e:
            if "HTTP 404" in str(e) and not self.tx:
                raise SourceError("No backend knows this transaction. It may have been replaced or dropped from the mempool.") from None
            if not self.tx:
                raise
        status = self.tx.get("status") or {}
        self.spends = await self._try("outspends", self.txid) or self.spends
        self.cpfp = await self._try("cpfp", self.txid) or self.cpfp
        self.rbf = await self._try("rbf", self.txid) or self.rbf
        if not status.get("confirmed"):
            await self._first_seen()
            if not self.hub.chain.mempool_blocks:
                self.mblocks = await self._try("mempool_blocks") or self.mblocks
        await self._tip()
        self.provs = [settled(p, float(status.get("block_time", 0))) for p in provs] if status.get("confirmed") else provs

    async def _first_seen(self) -> None:
        """`transaction-times` knows recently seen transactions only, and answers 0 for the rest. The brackets go out
        unescaped, as the API documents them."""
        for key in self.hub.bitcoin_order():
            src = self.hub.sources[key]
            if key in self.down or not hasattr(src, "tx_times"):
                continue
            try:
                times, _ = await src.get(f"/api/v1/transaction-times?txId[]={self.txid}", ttl=300)
            except SourceError:
                continue
            if isinstance(times, list) and times and times[0]:
                self.first_seen = float(times[0])
            return

    # what is on the page ────────────────────────────────────

    def _shown(self) -> tuple[list[dict], list[dict], list[dict], list[tuple[str, dict, dict | None]]]:
        package = [dict(t, relation="ancestor") for t in self.cpfp.get("ancestors") or []]
        package += [dict(t, relation="descendant") for t in self.cpfp.get("descendants") or []]
        tree = walk_tree(self.rbf["replacements"])[:60] if self.rbf.get("replacements") else []
        return self.tx.get("vin", [])[:MAX_ROWS], self.tx.get("vout", [])[:MAX_ROWS], package, tree

    def _targets(self) -> list[str | None]:
        ins, outs, package, tree = self._shown()
        go: list[str | None] = [None if i.get("is_coinbase") else f"TX {i['txid']}" for i in ins]
        go += [f"ADDR {o['scriptpubkey_address']}" if o.get("scriptpubkey_address") else None for o in outs]
        go += [f"TX {t['txid']}" for t in package]
        return go + [f"TX {n['tx']['txid']}" for _, n, _ in tree]

    def enter(self) -> str | None:
        go = self._targets()
        return go[self.cur] if self.cur < len(go) else None

    def hint(self) -> str:
        return "enter opens the input's tx or the output's address"

    def menu(self) -> list[tuple[str, str]]:
        height = (self.tx.get("status") or {}).get("block_height")
        return ([("block", f"BLK {height}")] if height else [("MEMP", "MEMP")]) + [("RBF", "RBF"), ("FEES", "FEES")]

    def export(self):
        rows = []
        for n, i in enumerate(self.tx.get("vin", [])):
            p = i.get("prevout") or {}
            kind = "coinbase" if i.get("is_coinbase") else script_type(p.get("scriptpubkey_type", ""))
            rows.append(["input", n, p.get("scriptpubkey_address", ""), kind, p.get("value"), f"{i.get('txid')}:{i.get('vout')}",
                         i.get("sequence")])
        for n, o in enumerate(self.tx.get("vout", [])):
            s = self.spends[n] if n < len(self.spends) else {}
            rows.append(["output", n, o.get("scriptpubkey_address", ""), script_type(o.get("scriptpubkey_type", "")), o.get("value"),
                         s.get("txid", "") if s.get("spent") else "", ""])
        return ["side", "index", "address", "script_type", "value_sat", "prev_or_spending_tx", "sequence"], rows

    # the sentence at the top ────────────────────────────────

    def summary(self) -> str:
        tx, status = self.tx, self.tx.get("status") or {}
        n_in, n_out = len(tx.get("vin", [])), len(tx.get("vout", []))
        parts = [f"{n_in:,} input{'s' if n_in != 1 else ''}", f"{n_out:,} output{'s' if n_out != 1 else ''}"]
        if is_coinbase(tx):
            parts = ["Coinbase: it creates the block's new coins and collects its fees", parts[1]]
        elif (r := rate_of(tx)) is not None:
            parts.append(f"pays {rate_words(r)} sat/vB")
        if status.get("confirmed"):
            conf = confirmations(self.height_now(), status.get("block_height"))
            parts.append(f"confirmed {conf - 1:,} block{'s' if conf != 2 else ''} ago" if conf > 1 else
                         "confirmed in the latest block" if conf == 1 else f"confirmed in block {status.get('block_height', 0):,}")
        else:
            parts.append("waiting in the mempool")
        if not is_coinbase(tx):
            parts.append("signals RBF" if signals_rbf(tx) else "does not signal RBF")
        n_ret = sum(1 for o in tx.get("vout", []) if o.get("scriptpubkey_type") == "op_return")
        if n_ret:
            parts.append("one OP_RETURN" if n_ret == 1 else f"{n_ret} OP_RETURN outputs")
        if self.rbf.get("replaces"):
            parts.append("replaced an earlier version")
        return ", ".join(parts) + "."

    def _position(self, rate: float) -> str:
        blocks = self.hub.chain.mempool_blocks or self.mblocks
        if not blocks:
            return ""
        spacing = (self.hub.chain.difficulty.get("timeAvg") or 600_000) / 60_000
        for i, b in enumerate(blocks):
            rng = b.get("feeRange") or []
            if rng and (rate >= float(rng[0]) or i == len(blocks) - 1):
                deep = i == len(blocks) - 1 and rate < float(rng[0])
                where = f"block {i + 1} of the queue" if not deep else f"behind {len(blocks)} blocks of higher fees"
                return f"about {where}, ~{(i + 1) * spacing:.0f}m at this epoch's pace" if not deep else where
        return ""

    # drawing ────────────────────────────────────────────────

    def draw(self, w: int, h: int) -> list[Text]:
        tx = self.tx
        if not tx:
            return []
        now, price = time.time(), self.hub.btc_price() if w >= 72 else None
        ins, outs, package, tree = self._shown()
        self.n_rows = len(ins) + len(outs) + len(package) + len(tree)
        out = self.privacy(w) + ui.wrap(self.summary(), w, style=f"bold {TEXT}") + [Text("")]
        out.append(fact("txid", self.txid, w, 9))

        out += self._facts(w, now)

        total_in = sum((i.get("prevout") or {}).get("value", 0) for i in tx.get("vin", []))
        total_out = sum(o.get("value", 0) for o in tx.get("vout", []))
        out += [Text(""), sect(f"INPUTS {len(tx.get('vin', [])):,}", w, fmt.sats(total_in) if total_in else "new coins")]
        first = len(out)
        rows = []
        for n, i in enumerate(ins):
            p = i.get("prevout") or {}
            v = p.get("value")
            who = "coinbase" if i.get("is_coinbase") else p.get("scriptpubkey_address") or script_type(p.get("scriptpubkey_type", ""))
            kind = "" if i.get("is_coinbase") else script_type(p.get("scriptpubkey_type", ""))
            rows.append([str(n), who, kind, sat(v) if v is not None else None,
                         ui.usd(v / 1e8 * price) if price and v is not None else None,
                         "" if i.get("is_coinbase") else f"{mid(i.get('txid', ''), 13)}:{i.get('vout')}"])
        out += fit(self._io_specs("from", "prev tx:out"), rows, w, self.cursor_in(0, len(ins)))
        self.line_of = {n: first + 2 + n for n in range(len(ins))}
        if len(tx.get("vin", [])) > len(ins):
            out.append(ui.note(f"+ {len(tx['vin']) - len(ins):,} more inputs. EXP exports every one."))

        out += [Text(""), sect(f"OUTPUTS {len(tx.get('vout', [])):,}", w, fmt.sats(total_out))]
        first, rows, texts = len(out), [], []
        for n, o in enumerate(outs):
            kind, v = o.get("scriptpubkey_type", ""), o.get("value", 0)
            s = self.spends[n] if n < len(self.spends) else None
            if kind == "op_return":
                texts.append((n, *op_return_text(o.get("scriptpubkey", ""))))
                fate = Text("data, unspendable", style=FAINT)
            elif s is None:
                fate = Text(DASH, style=FAINT)
            elif s.get("spent"):
                fate = Text(f"spent by {mid(s.get('txid', ''), 13)}" if w >= 100 else "spent", style=DIM)
            else:
                fate = Text("unspent", style=GREEN)
            rows.append([str(n), o.get("scriptpubkey_address") or ("OP_RETURN" if kind == "op_return" else script_type(kind)), script_type(kind),
                         sat(v), ui.usd(v / 1e8 * price) if price else None, fate])
        out += fit(self._io_specs("to", "fate"), rows, w, self.cursor_in(len(ins), len(outs)))
        self.line_of.update({len(ins) + n: first + 2 + n for n in range(len(outs))})
        if len(tx.get("vout", [])) > len(outs):
            out.append(ui.note(f"+ {len(tx['vout']) - len(outs):,} more outputs. EXP exports every one."))

        for n, payload, is_text in texts[:4]:
            out += [Text(""), sect(f"OP_RETURN · output {n}", w, "text" if is_text else "hex: not printable UTF-8")]
            out += [Text(c, style=f"bold {ui.CYAN}" if is_text else DIM, no_wrap=True) for c in chunks(payload, w)]

        at = len(ins) + len(outs)
        if package:
            out += [Text("")] + self._package(package, w, at, len(out) + 1)
        if tree:
            out += [Text("")] + self._timeline(tree, w, at + len(package), len(out) + 1, now)
        self.keep_in_view(self.line_of.get(self.cur, 0), h)
        return out

    def _facts(self, w: int, now: float) -> list[Text]:
        """Status, fee, size and the RBF signal: label and value rows that fold onto more lines as the pane narrows."""
        tx, status = self.tx, self.tx.get("status") or {}
        rate, eff = rate_of(tx), self.cpfp.get("effectiveFeePerVsize")
        if status.get("confirmed"):
            conf, mined = confirmations(self.height_now(), status.get("block_height")), float(status.get("block_time", 0))
            parts = [ui.kv(f"{'status':<9}", "confirmed", f"bold {GREEN}"),
                     ui.kv("block", f"{int(status.get('block_height', 0)):,}", f"bold {ORANGE}")]
            if conf:
                parts.append(ui.kv("confirmations", f"{conf:,}"))
            out = flow(parts + [Text(f"{utc(mined, w >= 60)} · {ui.when(mined, now)} ago", style=DIM)], w, 10)
        else:
            seen = Text("first seen: not known to this backend", style=FAINT)
            if self.first_seen:
                seen = Text(f"first seen {utc(self.first_seen, False)} UTC · {ui.when(self.first_seen, now)} ago", style=DIM)
            out = flow([ui.kv(f"{'status':<9}", "in the mempool", f"bold {ui.YELLOW}"), seen], w, 10)
            if rate is not None and (where := self._position(float(eff or rate))):
                out += ui.wrap(where, w, style=DIM, indent=" " * 10)
        if not is_coinbase(tx):
            parts = [ui.kv(f"{'fee':<9}", ui.sats_usd(tx.get("fee", 0), self.hub.btc_price())), ui.kv("rate", exact_rate(rate, " sat/vB"))]
            if eff and rate and abs(float(eff) - rate) > 0.005:
                parts.append(ui.kv("effective", exact_rate(float(eff), " sat/vB with its package")))
            out += flow(parts, w, 10)
        parts = [ui.kv(f"{'size':<9}", f"{vsize_of(tx):,.2f} vB".replace(".00 ", " ")), ui.kv("weight", f"{int(tx.get('weight', 0)):,} WU"),
                 ui.kv("bytes", f"{int(tx.get('size', 0)):,}"), ui.kv("version", str(tx.get("version", DASH))),
                 ui.kv("locktime", f"{int(tx.get('locktime', 0)):,}")]
        if "sigops" in tx:
            parts.append(ui.kv("sigops", str(tx["sigops"])))
        out += flow(parts, w, 10)
        if not is_coinbase(tx):
            seqs = [int(i.get("sequence", 0xFFFFFFFF)) for i in tx.get("vin", [])]
            signal = ("signals replaceability", f"bold {ui.YELLOW}") if signals_rbf(tx) else ("no signal", f"bold {TEXT}")
            out.append(ui.t((f"{'RBF':<10}", FAINT), signal, (f" · lowest sequence 0x{min(seqs):08x}" if seqs and w >= 60 else "", FAINT)))
        return out

    @staticmethod
    def _io_specs(who: str, last: str) -> list[C]:
        return [C(ui.Col("#", style=FAINT), 3), C(ui.Col(who, "left"), 0, elastic=13), C(ui.Col("type", "left", style=DIM), 2),
                C(ui.Col("amount"), 0), C(ui.Col("usd", style=DIM), 5), C(ui.Col(last, "left", style=DIM), 4)]

    def _package(self, package: list[dict], w: int, at: int, first_line: int) -> list[Text]:
        eff, alone = self.cpfp.get("effectiveFeePerVsize"), rate_of(self.tx)
        out = [sect("CPFP PACKAGE", w, f"effective {rate_words(float(eff))} sat/vB" if eff else "")]
        if eff and alone:
            words = (f"Alone this transaction pays {rate_words(alone)} sat/vB. Miners weigh it with its unconfirmed relatives: "
                     f"{rate_words(float(eff))} sat/vB together.")
            out += ui.wrap(words, w, style=DIM)
        rows = [[t["relation"], t["txid"], sat(t.get("fee", 0)), f"{t.get('weight', 0) / 4:,.0f}",
                 rate_text(t.get("fee", 0) / (t["weight"] / 4) if t.get("weight") else None)] for t in package]
        specs = [C(ui.Col("relation", "left", style=DIM), 3), C(ui.Col("txid", "left"), 0, elastic=13), C(ui.Col("fee"), 1),
                 C(ui.Col("vB", style=DIM), 2), C(ui.Col("s/vB"), 0)]
        self.line_of.update({at + n: first_line + len(out) + 2 + n for n in range(len(package))})
        return out + fit(specs, rows, w, self.cursor_in(at, len(package)))

    def _timeline(self, tree: list[tuple[str, dict, dict | None]], w: int, at: int, first_line: int, now: float) -> list[Text]:
        out = [sect("REPLACEMENTS", w, f"{len(tree) - 1} replaced · sat/vB · newest first")]
        id_w = 17 if w >= 84 else 11
        for n, (lead, node, newer) in enumerate(tree):
            t = node.get("tx") or {}
            line = ui.t((lead, FAINT), (mid(t.get("txid", ""), id_w), f"bold {ORANGE}" if t.get("txid") == self.txid else TEXT), ("  ", ""),
                        exact_rate(float(t.get("rate", 0))))
            if w >= 64:
                line.append_text(ui.t(("  ", ""), sat(t.get("fee", 0)), (f"  {clock(float(node.get('time', 0)))}", DIM)))
            if newer is not None:                           # this version was replaced: by how much, and how long it lasted
                d_rate, d_frac, d_fee = fee_bump(t, newer.get("tx") or {})
                gap = node.get("interval") or float(newer.get("time", 0)) - float(node.get("time", 0))
                line.append_text(ui.t((f"  +{d_rate:.2f}", f"bold {GREEN}"), (f" +{d_frac:.1%}", GREEN),
                                      (f" +{fmt.sats(d_fee)}" if w >= 100 else "", DIM),
                                      (f"  after {gap_text(gap)}" if w >= 64 else f" {gap_text(gap)}", DIM)))
            else:
                mined = bool(node.get("mined") or t.get("mined"))
                line.append("  mined" if mined else "  latest", style=f"bold {GREEN}" if mined else FAINT)
            if node.get("fullRbf") and w >= 72:
                line.append("  full-RBF", style=ui.VIOLET)
            if self.cur == at + n:
                line.pad_right(max(w - line.cell_len, 0))
                line.stylize(f"on {ui.CURSOR_BG}")
            line.truncate(w)
            self.line_of[at + n] = first_line + len(out)
            out.append(line)
        return out


# ── ADDR ────────────────────────────────────────────────────

def net_effect(tx: dict, address: str) -> int:
    """What this transaction did to the address: what it received minus what it spent, in sats."""
    got = sum(o.get("value", 0) for o in tx.get("vout", []) if o.get("scriptpubkey_address") == address)
    gave = sum((i.get("prevout") or {}).get("value", 0) for i in tx.get("vin",
            []) if (i.get("prevout") or {}).get("scriptpubkey_address") == address)
    return got - gave


WATCH_MEANS = ("Watching writes this address into wallet.watch in terminal.toml. WAL then asks your Bitcoin backend for its balance on every "
               "refresh. On a public backend that tells the operator, again and again, that you care about this address. With your own "
               "node or Tor (SET) nobody learns it. Nothing is signed or spent: the terminal holds no keys.")


class AddrPane(ChainPane):
    code, selectable = "ADDR", True
    TOP_UTXOS = 8

    def __init__(self, hub, security=None, args=()) -> None:
        super().__init__(hub, security, args)
        a = self.args[0] if self.args else ""
        self.address = a.lower() if a.lower().startswith(("bc1", "tb1")) else a
        self.info: dict = {}
        self.history: list[dict] = []
        self.utxos: list[dict] | None = None
        self.utxo_error = ""
        self.all_utxos = False
        self.want_older = False
        self.ended = False
        self.effects: dict[str, tuple[int, float | None]] = {}     # txid -> (net effect on this address, fee rate), worked out once
        self.asking = False                     # `w` was pressed once: the explanation is on screen
        self.said = ""
        self.title = f"address {mid(self.address, 17)}" if self.address else "address"

    async def load(self) -> None:
        self.start_load()
        if not 14 <= len(self.address) <= 90 or not self.address.isalnum():
            raise SourceError("ADDR takes a Bitcoin address: ADDR <address>")
        provs: list[Provenance] = []
        self.info = await self._ask("address", self.address, provs=provs, private=True)
        page = await self._try("address_txs", self.address, provs=provs)
        if page is not None:
            known = {t["txid"] for t in page}
            self.history = list(page) + [t for t in self.history if t["txid"] not in known]
        if self.want_older:
            await self._older_page()
        try:
            self.utxos, self.utxo_error = await self._ask("address_utxo", self.address, provs=provs), ""
        except SourceError as e:
            self.utxos = None
            self.utxo_error = ("The backend lists UTXOs only for addresses with 500 or fewer, and this one has more. The balance above is exact."
                               if "HTTP 400" in str(e) else f"UTXOs unavailable: {e}")
        await self._tip()
        await asyncio.to_thread(self._digest)               # a page of history can be megabytes of inputs: off the event loop
        self.provs = provs

    def _digest(self) -> None:
        for t in self.history:
            if t["txid"] not in self.effects or not (t.get("status") or {}).get("confirmed"):
                self.effects[t["txid"]] = (net_effect(t, self.address), rate_of(t))

    async def _older_page(self) -> None:
        """`/txs/chain/{last_txid}` continues the confirmed history below the oldest transaction already loaded."""
        self.want_older = False
        confirmed = [t for t in self.history if (t.get("status") or {}).get("confirmed")]
        if not confirmed:
            return
        path = f"/api/address/{self.address}/txs/chain/{confirmed[-1]['txid']}"
        for key in self.hub.bitcoin_order():
            if key in self.down:
                continue
            try:
                page, _ = await self.hub.sources[key].get(path, ttl=60)
            except (SourceError, AttributeError):
                continue
            known = {t["txid"] for t in self.history}
            fresh = [t for t in page if t["txid"] not in known] if isinstance(page, list) else []
            if fresh:
                self.cur, self.moved = len(self._utxo_rows()) + len(self.history), True
            self.history += fresh
            self.ended = not fresh
            return

    # rows and keys ──────────────────────────────────────────

    def _utxo_rows(self) -> list[dict]:
        rows = sorted(self.utxos or [], key=lambda u: -u.get("value", 0))
        return rows if self.all_utxos else rows[:self.TOP_UTXOS]

    def enter(self) -> str | None:
        rows = self._utxo_rows() + self.history
        return f"TX {rows[self.cur]['txid']}" if self.cur < len(rows) else None

    def key(self, k: str, ch: str | None) -> bool:
        if ch == "w":
            self._watch()
        elif self.asking:
            self.asking, self.said = False, "Not added."
        elif ch == "u":
            self.all_utxos, self.cur = not self.all_utxos, 0
        elif ch == "]" and not self.ended and not self.want_older:
            self.want_older, self.loaded_at = True, 0.0
        else:
            return False
        return True

    def _watch(self) -> None:
        watch = self.hub.cfg.setdefault("wallet", {}).setdefault("watch", [])
        if self.address in watch:
            self.asking, self.said = False, "Already watched. WAL shows it. Edit wallet.watch in terminal.toml to remove it."
        elif not self.asking:
            self.asking, self.said, self.top = True, "", 0
        else:
            watch.append(self.address)
            config.save(self.hub.cfg)
            self.asking, self.said = False, f"Watching {mid(self.address, 21)}. It is in wallet.watch now."

    def hint(self) -> str:
        return "w again adds it · any other key cancels" if self.asking else "enter opens the tx · w watch · u all UTXOs · ] older"

    def menu(self) -> list[tuple[str, str]]:
        return [("WAL", "WAL"), ("RBF", "RBF"), ("SET", "SET"), ("MEMP", "MEMP")]

    def export(self):
        rows = [["utxo", u["txid"], u.get("vout"), (u.get("status") or {}).get("block_height"), (u.get("status") or {}).get("block_time"),
                 u.get("value")] for u in sorted(self.utxos or [], key=lambda u: -u.get("value", 0))]
        rows += [["tx", t["txid"], "", (t.get("status") or {}).get("block_height"), (t.get("status") or {}).get("block_time"),
                  net_effect(t, self.address)] for t in self.history]
        return ["kind", "txid", "vout", "block_height", "block_time", "value_or_net_sat"], rows

    # drawing ────────────────────────────────────────────────

    def draw(self, w: int, h: int) -> list[Text]:
        if not self.info:
            return []
        now, price = time.time(), self.hub.btc_price()
        out = self.privacy(w)
        if self.asking:
            out += [sect("WATCH THIS ADDRESS?", w)] + ui.wrap(WATCH_MEANS, w, style=TEXT)
            out += [ui.t(("w", f"bold {ORANGE}"), (" adds it   ", DIM), ("any other key", f"bold {ORANGE}"), (" cancels", DIM)), Text("")]
        elif self.said:
            out += ui.wrap(self.said, w, style=f"bold {GREEN}" if self.said.startswith("Watching") else DIM) + [Text("")]
        out += self._summary(w, now, price)
        utxos = self._utxo_rows()
        self.line_of = {}
        out += [Text("")] + self._utxo_table(utxos, w, len(out) + 1, price)
        out += [Text("")] + self._history_table(w, len(out) + 1, len(utxos), price)
        self.n_rows = len(utxos) + len(self.history)
        if not self.asking:
            self.keep_in_view(self.line_of.get(self.cur, 0), h)
        return out

    def _summary(self, w: int, now: float, price: float | None) -> list[Text]:
        chain, pool = self.info.get("chain_stats") or {}, self.info.get("mempool_stats") or {}
        funded, spent = chain.get("funded_txo_sum", 0), chain.get("spent_txo_sum", 0)
        pending = pool.get("funded_txo_sum", 0) - pool.get("spent_txo_sum", 0)
        n_out, n_spent = chain.get("funded_txo_count", 0), chain.get("spent_txo_count", 0)
        wide = price if w >= 72 else None
        moving = Text(fmt.sats(pending, signed=True), style=f"bold {GREEN if pending > 0 else RED if pending < 0 else DIM}")
        out = [fact("address", self.address, w, 9)]
        out += flow([ui.kv(f"{'balance':<9}", ui.sats_usd(funded - spent, price)), ui.kv("mempool", moving),
                     ui.kv("BTC", f"{(funded - spent) / 1e8:,.8f}")], w, 10)
        out += flow([ui.kv(f"{'received':<9}", ui.sats_usd(funded, wide)), ui.kv("sent", ui.sats_usd(spent, wide))], w, 10)
        out += flow([ui.kv(f"{'activity':<9}", f"{self._n_tx():,} tx"), ui.kv("funded outputs", f"{n_out:,}"), ui.kv("spent", f"{n_spent:,}"),
                     ui.kv("unspent", f"{n_out - n_spent:,}")], w, 10)
        times = [float((t.get("status") or {}).get("block_time") or 0) for t in self.history if (t.get("status") or {}).get("confirmed")]
        if times:
            whole = self.ended or len(self.history) >= self._n_tx()
            oldest = ui.kv("first seen", ui.day(min(times))) if whole else ui.kv("oldest loaded", f"{ui.day(min(times))} · ] loads older")
            out += flow([ui.kv(f"{'last seen':<9}", f"{ui.day(max(times))} · {ui.when(max(times), now)} ago"), oldest], w, 10)
        if wide:
            out.append(ui.note("Dollar values use the price now, not the price on the day."))
        return out

    def _n_tx(self) -> int:
        return sum(int((self.info.get(k) or {}).get("tx_count", 0)) for k in ("chain_stats", "mempool_stats"))

    def _utxo_table(self, utxos: list[dict], w: int, first_line: int, price: float | None) -> list[Text]:
        if self.utxos is None:
            return [sect("UTXOS", w)] + ui.wrap(self.utxo_error or "UTXOs unavailable.", w, style=DIM)
        n = len(self.utxos)
        out = [sect("UTXOS", w, f"{len(utxos)} largest of {n:,} · u shows all" if n > len(utxos) else f"{n:,}")]
        if not utxos:
            return out + [ui.note("Nothing unspent.")]
        tip, rows = self.height_now(), []
        for u in utxos:
            st = u.get("status") or {}
            conf = confirmations(tip, st.get("block_height"))
            rows.append([f"{u['txid']}:{u.get('vout')}", sat(u.get("value", 0)), ui.usd(u.get("value", 0) / 1e8 * price) if price else None,
                         f"{int(st['block_height']):,}" if st.get("block_height") else "mempool", f"{conf:,}" if conf else DASH,
                         ui.day(float(st["block_time"])) if st.get("block_time") else DASH])
        specs = [C(ui.Col("outpoint", "left"), 0, elastic=15), C(ui.Col("value"), 0), C(ui.Col("usd", style=DIM), 3),
                 C(ui.Col("block", style=DIM), 2), C(ui.Col("conf", style=DIM), 5), C(ui.Col("received", style=FAINT), 4)]
        self.line_of.update({i: first_line + 3 + i for i in range(len(utxos))})
        return out + fit(specs, rows, w, self.cursor_in(0, len(utxos)))

    def _history_table(self, w: int, first_line: int, at: int, price: float | None) -> list[Text]:
        out = [sect("HISTORY", w, f"{len(self.history):,} of {self._n_tx():,} loaded · newest first")]
        if not self.history:
            return out + [ui.note("No transactions yet." if not self._n_tx() else "The backend did not return the history.")]
        tip, rows = self.height_now(), []
        for t in self.history:
            st = t.get("status") or {}
            bh = st.get("block_height")
            net, rate = self.effects.get(t["txid"]) or (net_effect(t, self.address), rate_of(t))
            rows.append([ui.day(float(st["block_time"])) if st.get("block_time") else "mempool", t["txid"],
                         Text(fmt.sats(net, signed=True), style=f"bold {GREEN if net > 0 else RED if net < 0 else DIM}"),
                         ui.usd(abs(net) / 1e8 * price) if price else None, f"{int(bh):,}" if bh else DASH, rate_text(rate),
                         f"{confirmations(tip, bh):,}" if bh and tip else DASH])
        specs = [C(ui.Col("date", "left", style=DIM), 1), C(ui.Col("txid", "left"), 0, elastic=13), C(ui.Col("net effect"), 0),
                 C(ui.Col("usd now", style=DIM), 4), C(ui.Col("block", style=DIM), 3), C(ui.Col("s/vB"), 5), C(ui.Col("conf", style=FAINT), 6)]
        self.line_of.update({at + i: first_line + 3 + i for i in range(len(self.history))})
        out += fit(specs, rows, w, self.cursor_in(at, len(self.history)))
        return out + ([ui.note("loading older transactions…")] if self.want_older else [])


# ── RBF ─────────────────────────────────────────────────────

def tree_stats(tree: dict) -> dict[str, Any]:
    """One replacement tree as a row: the newest version against the one it replaced, and the whole history's size."""
    flat = walk_tree(tree)
    new, at = tree.get("tx") or {}, float(tree.get("time", 0))
    prev = max(tree.get("replaces") or [], key=lambda k: float(k.get("time", 0)), default=None)
    old = (prev or {}).get("tx") or {}
    d_rate, d_frac, d_fee = fee_bump(old, new) if old else (0.0, 0.0, 0.0)
    first = min((n for _, n, _ in flat), key=lambda n: float(n.get("time", 0)))
    gap = float(prev.get("interval") or at - float(prev.get("time", 0))) if prev else 0.0
    return {"txid": new.get("txid", ""), "time": at, "old": float(old.get("rate", 0)) if old else None, "new": float(new.get("rate", 0)),
            "d_rate": d_rate, "d_frac": d_frac, "d_fee": d_fee, "fee": float(new.get("fee", 0)), "gap": gap, "count": len(flat) - 1,
            "first": float((first.get("tx") or {}).get("rate", 0)), "span": at - float(first.get("time", 0)),
            "full": any(n.get("fullRbf") for _, n, _ in flat), "mined": bool(tree.get("mined") or new.get("mined")),
            "value": float(new.get("value", 0))}


class RbfPane(ChainPane):
    code, selectable, every, tick = "RBF", True, 15, 1.0

    def __init__(self, hub, security=None, args=()) -> None:
        super().__init__(hub, security, args)
        self.full = bool(self.args) and self.args[0].lower() in ("full", "fullrbf", "f")
        self.rest: dict[bool, list[dict]] = {False: [], True: []}
        self.rest_prov: dict[bool, Provenance] = {}
        self._stream_ref: Any = None
        self._stream_at = 0.0
        self.title = "replacements"

    def on_mount(self) -> None:
        self.hub.track("rbf", True)                 # 2.5 MB a minute: held only while this pane is open

    def on_unmount(self) -> None:
        self.hub.track("rbf", False)

    async def load(self) -> None:
        self.start_load()
        self.title = "full-RBF replacements" if self.full else "replacements"
        if self.hub.rbf_latest and not self.full:
            return                                   # the stream is feeding the page
        provs: list[Provenance] = []
        try:
            self.rest[self.full] = await self._ask("replacements", self.full, provs=provs)
            self.rest_prov[self.full] = provs[0]
        except SourceError:
            if not self.rest[self.full] and not self.hub.rbf_latest:
                raise

    def _trees(self) -> list[dict]:
        live = self.hub.rbf_latest
        if self.full:
            return self.rest[True] or [t for t in live if tree_stats(t)["full"]]
        return live or self.rest[False]

    def cache_key(self) -> tuple:
        return (id(self.hub.rbf_latest), len(self.hub.rbf_latest), self.full)

    def key(self, k: str, ch: str | None) -> bool:
        if ch != "f":
            return False
        self.full, self.cur, self.loaded_at = not self.full, 0, 0.0
        self.title = "full-RBF replacements" if self.full else "replacements"
        return True

    def enter(self) -> str | None:
        trees = self._trees()
        return f"TX {(trees[self.cur].get('tx') or {}).get('txid')}" if self.cur < len(trees) else None

    def hint(self) -> str:
        return "enter opens the tx · f " + ("every replacement" if self.full else "full-RBF only")

    def menu(self) -> list[tuple[str, str]]:
        return [("MEMP", "MEMP"), ("FEES", "FEES"), ("BLK", "BLK")]

    def export(self):
        header = ["txid", "time", "old_sat_per_vb", "new_sat_per_vb", "bump_sat_per_vb", "bump_pct", "bump_fee_sat", "seconds_between",
                  "replacements", "first_sat_per_vb", "full_rbf", "mined"]
        return header, [[r["txid"], r["time"], r["old"], r["new"], r["d_rate"], r["d_frac"] * 100, r["d_fee"], r["gap"], r["count"], r["first"],
                         r["full"], r["mined"]] for r in map(tree_stats, self._trees())]

    def draw(self, w: int, h: int) -> list[Text]:
        now, live = time.time(), self.hub.rbf_latest
        if live is not self._stream_ref:
            self._stream_ref, self._stream_at = live, now
        streaming = bool(live) and not (self.full and self.rest[True])
        if streaming:
            base = self.hub.sources[self.hub.bitcoin_order()[0]].name if self.hub.bitcoin_order() else "mempool"
            self.provs = [Provenance(f"{base} ws", self._stream_at, self._stream_at, "live")]
        else:
            self.provs = [p for p in (self.rest_prov.get(self.full),) if p]
        trees = self._trees()
        self.n_rows = len(trees)
        head = sect("FULL-RBF REPLACEMENTS" if self.full else "REPLACEMENTS", w,
                ("stream" if streaming else "polled every 15 s") + " · newest first")
        if not trees:
            nothing = "No full-RBF replacements right now. f shows every replacement." if self.full else "No replacements right now."
            return [head, ui.note(nothing)] if self.loaded_at else []
        stats, rows = [tree_stats(t) for t in trees], []
        for r in stats:
            change = Text(no_wrap=True)
            change.append_text(exact_rate(r["old"]))
            change.append(" → ", style=FAINT)
            change.append_text(exact_rate(r["new"]))
            flags = Text(no_wrap=True)
            flags.append("full " if r["full"] else "", style=ui.VIOLET)
            flags.append("mined" if r["mined"] else "", style=GREEN)
            had = r["old"] is not None                      # a tree with nothing under it has no bump to show
            rows.append([clock(r["time"]), ui.when(r["time"], now), r["txid"], change,
                         Text(f"+{r['d_rate']:.2f}", style=f"bold {GREEN}") if had else None,
                         Text(f"+{r['d_frac']:.1%}", style=GREEN) if had else None,
                         gap_text(r["gap"]) if had else DASH, str(r["count"]), flags, sat(r["d_fee"]) if had else None, rate_text(r["first"]),
                         sat(r["value"])])
        specs = [C(ui.Col("UTC", "left", style=DIM), 5), C(ui.Col("age", style=FAINT), 8), C(ui.Col("txid", "left"), 0, elastic=11),
                 C(ui.Col("old → new s/vB"), 0), C(ui.Col("bump"), 1), C(ui.Col("%"), 2), C(ui.Col("after", style=DIM), 3),
                 C(ui.Col("n", style=DIM), 4), C(ui.Col("flags", "left"), 6), C(ui.Col("fee added"), 7), C(ui.Col("first s/vB"), 9),
                 C(ui.Col("value"), 10)]
        out = [head] + fit(specs, rows, w, self.cur)
        self.follow(self.cur + 3, h)
        if h - len(out) >= 3:
            med = sorted(r["d_frac"] for r in stats)[len(stats) // 2]
            out += [Text("")] + flow([ui.kv("trees", str(len(stats))), ui.kv("median bump", f"+{med:.1%}"),
                                      ui.kv("full-RBF", str(sum(1 for r in stats if r["full"]))),
                                      ui.kv("mined", str(sum(1 for r in stats if r["mined"])))], w)
        return out


# ── registration ────────────────────────────────────────────

_DOWN = ("Backends are asked in the order of bitcoin.order, and the frame names the one that answered. Esplora has no pool, audit, CPFP or "
         "replacement data, so those parts stay empty when it is the one answering. With no backend the last picture stays, dimmed, with "
         "its age.")
_PRIVATE = ("A lookup on a public backend tells its operator what you looked up. The first one says so, once. Your own mempool, Esplora or "
            "node in SET, or a SOCKS5 proxy (Tor), keeps it private.")

register(Function(
    "BLK", "Blocks", "Bitcoin", "recent blocks, or one block: header, fee percentiles, coinbase tag, reward, transactions", BlkPane,
    args="[height or hash]", needs=("blocks", "block", "block_txs"),
    help="With no argument: the latest blocks, newest first, with age, pool, transaction count, size in MB, weight in MWU, total fees and "
         "reward in BTC, the median fee rate in sat/vB and the audit health, which is how closely the block matched the template "
         "mempool.space expected (extras.matchRate). Enter opens the block under the cursor. n, or moving past the last row, loads the "
         "15 older blocks. p moves back a page. Data: /api/v1/blocks, refreshed every 60 s. "
         "With a height or a hash: the header (hash, time in UTC, version, bits, nonce, difficulty, merkle root), the seven fee-rate "
         "percentiles of extras.feeRange coloured on the fee ramp, the readable part of the coinbase script, and the reward split into "
         "subsidy and fees with the fee share. Below are its transactions, 25 a page. ] and [ turn the page and Enter opens one in TX. "
         "Digits 1 and 2 open the previous and the next block. Data: /api/block-height/{height}, /api/v1/block/{hash} and "
         "/api/block/{hash}/txs/{start}. A mined block never changes, so it is cached for good. " + _DOWN))
register(Function(
    "TX", "Transaction", "Bitcoin", "explains any transaction: status, fee, inputs, outputs, OP_RETURN, CPFP, replacements", TxPane,
    args="<txid>", needs=("tx", "outspends", "cpfp", "rbf"),
    help="The first line says what the transaction is in plain words. Then its status: the block and the confirmations counted from the "
         "live tip, or, in the mempool, when it was first seen (/api/v1/transaction-times, which only knows recent transactions) and "
         "about which projected block its fee rate lands in. Fee in sats with dollars beside it, fee rate in sat/vB (fee over weight/4), "
         "virtual size, weight, version and locktime. It signals RBF when any input sequence is below 0xfffffffe. Inputs and outputs show "
         "script type, address and amount, and each output says whether it is spent (/api/tx/{txid}/outspends). An OP_RETURN payload is shown "
         "as text when it is printable UTF-8 of single-width characters, otherwise as hex. The CPFP section lists unconfirmed ancestors and "
         "descendants and the effective rate miners see (/api/v1/cpfp). The replacement section draws every earlier version, the fee added "
         "and the seconds between versions (/api/v1/tx/{txid}/rbf). Enter on an input opens its previous transaction, on an output its "
         "address, on a package or replacement row that transaction. Dollar values use the price now. Refreshed every 60 s. "
         + _PRIVATE + " " + _DOWN))
register(Function(
    "ADDR", "Address", "Bitcoin", "an address: balance, totals, UTXOs, history with the net effect of each transaction, watch it", AddrPane,
    args="<address>", needs=("address", "address_txs", "address_utxo"),
    help="Balance is everything the address received minus everything it spent, confirmed, with the unconfirmed change beside it "
         "(/api/address/{a}: chain_stats and mempool_stats). Received and sent are lifetime totals. The UTXO table shows the eight largest "
         "unspent outputs, and u shows them all. mempool.space refuses the list above 500 UTXOs, and the page says so and keeps the exact "
         "balance. History is newest first with what each transaction did to this address: received minus spent, in sats. ] loads the next "
         "older page. First seen is exact once the whole history is loaded. Until then the page says oldest loaded. Enter opens the "
         "transaction under the cursor. w explains what watching means and a second w adds the address to wallet.watch, which WAL reads. "
         "Dollar values use the price now, not the price on the day. Refreshed every 60 s. " + _PRIVATE + " " + _DOWN))
register(Function(
    "RBF", "Replacements", "Bitcoin", "live fee bumps: old and new fee rate, the bump, seconds between versions, full-RBF", RbfPane,
    args="[full]", needs=("replacements",),
    help="Every row is one replacement tree, newest first: the time of the newest version in UTC, the fee rate of the version it replaced "
         "and its own in sat/vB, the bump in sat/vB and in percent, how long the replaced version lasted, how many versions were replaced "
         "in all, and whether any of them ignored the RBF signal (full-RBF) or the newest was mined. Wide panes add the fee added in sats, "
         "the first version's rate and the value moved. Enter opens the newest version in TX, where the whole tree is drawn. f switches to "
         "full-RBF replacements only. While this page is open the terminal subscribes to track-rbf on the mempool WebSocket, about 2.5 MB a "
         "minute, and unsubscribes when the last RBF pane closes. Without the stream it polls /api/v1/replacements or "
         "/api/v1/fullrbf/replacements every 15 s, and the section head says which. " + _DOWN))
