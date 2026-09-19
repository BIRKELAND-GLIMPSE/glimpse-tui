"""Panes and launchpads (TERMINAL.md 3.2): up to nine tiled panes, each running one function against its own security.

A launchpad is a tree of splits with a command at every leaf. `TileLayout` turns the tree into rectangles, so
splitting, closing, zooming and evening out never re-mount a widget. `FuncPane` is the base of every function
page: it draws its own frame (number, amber function chip, security, then source and delay on the right),
loads through the hub off the paint path, and renders from whatever it last loaded.
"""
from __future__ import annotations

import time
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass, field
from importlib import resources
from typing import TYPE_CHECKING, Any, ClassVar

from rich.text import Text
from textual.geometry import Offset, Region, Size, Spacing
from textual.layout import ArrangeResult, Layout, WidgetPlacement
from textual.widget import Widget

from ..data.core import Provenance, SourceError, ago
from ..theme import DIM, FAINT, ORANGE, RED, RULE, TEXT
from . import config, ui
from .instruments import Instrument

if TYPE_CHECKING:
    from .hub import Hub

MAX_PANES = 9
INK = "#0D0D0D"
SHIPPED = ("BTC", "CHAIN", "MINER", "MACRO", "TRADER", "TREASURY")


# ── the split tree ──────────────────────────────────────────

@dataclass
class Leaf:
    command: str
    pane: FuncPane | None = None


@dataclass
class Split:
    dir: str                                    # "row": side by side · "col": stacked
    children: list[Leaf | Split] = field(default_factory=list)
    weights: list[float] = field(default_factory=list)


Node = Leaf | Split


def leaves(node: Node) -> list[Leaf]:
    return [node] if isinstance(node, Leaf) else [lf for c in node.children for lf in leaves(c)]


def rects(node: Node, x: int, y: int, w: int, h: int) -> list[tuple[Leaf, Region]]:
    """Whole-cell rectangles for every leaf. The last child of a split takes the rounding remainder."""
    if isinstance(node, Leaf):
        return [(node, Region(x, y, max(w, 0), max(h, 0)))]
    out, total, at = [], sum(node.weights) or 1.0, 0
    span = w if node.dir == "row" else h
    for i, (child, weight) in enumerate(zip(node.children, node.weights, strict=True)):
        size = span - at if i == len(node.children) - 1 else round(span * weight / total)
        out += rects(child, x + at, y, size, h) if node.dir == "row" else rects(child, x, y + at, w, size)
        at += size
    return out


def _parent(root: Node, target: Node) -> Split | None:
    if isinstance(root, Split):
        for c in root.children:
            if c is target:
                return root
            if hit := _parent(c, target):
                return hit
    return None


def split(root: Node, at: Leaf, new: Leaf, direction: str) -> Node:
    """Split `at` in two. `s` stacks (col), `v` puts them side by side (row), as in vim."""
    parent = _parent(root, at)
    if parent and parent.dir == direction:
        i = parent.children.index(at)
        parent.children.insert(i + 1, new)
        parent.weights[i] /= 2
        parent.weights.insert(i + 1, parent.weights[i])
        return root
    node = Split(direction, [at, new], [1.0, 1.0])
    if parent is None:
        return node
    parent.children[parent.children.index(at)] = node
    return root


def close(root: Node, at: Leaf) -> Node | None:
    parent = _parent(root, at)
    if parent is None:
        return None
    i = parent.children.index(at)
    del parent.children[i], parent.weights[i]
    if len(parent.children) == 1:                       # a split of one collapses into its child
        only, grand = parent.children[0], _parent(root, parent)
        if grand is None:
            return only
        grand.children[grand.children.index(parent)] = only
    return root


def even(node: Node) -> None:
    if isinstance(node, Split):
        node.weights = [1.0] * len(node.children)
        for c in node.children:
            even(c)


def to_doc(node: Node) -> Any:
    if isinstance(node, Leaf):
        return node.command
    return {"split": node.dir, "weights": [round(w, 3) for w in node.weights], "panes": [to_doc(c) for c in node.children]}


def from_doc(doc: Any) -> Node:
    if isinstance(doc, str):
        return Leaf(doc)
    kids = [from_doc(c) for c in doc.get("panes", [])][:MAX_PANES] or [Leaf("HELP")]
    weights = [float(w) for w in doc.get("weights", [])]
    weights = weights if len(weights) == len(kids) and all(w > 0 for w in weights) else [1.0] * len(kids)
    return Split("col" if doc.get("split") == "col" else "row", kids, weights)


# ── launchpads on disk ──────────────────────────────────────

def layouts_dir():
    return config.config_dir() / "layouts"


def layout_names() -> list[str]:
    mine = sorted(p.stem.upper() for p in layouts_dir().glob("*.toml")) if layouts_dir().is_dir() else []
    return list(SHIPPED) + [n for n in mine if n not in SHIPPED]


def load_layout(name: str) -> Node | None:
    """The user's file wins over the shipped one of the same name."""
    text = None
    try:
        text = (layouts_dir() / f"{name.lower()}.toml").read_text()
    except OSError:
        try:
            text = resources.files("glimpse_tui.launchpads").joinpath(f"{name.lower()}.toml").read_text()
        except (OSError, ModuleNotFoundError):
            return None
    try:
        return from_doc(tomllib.loads(text)["layout"])
    except (tomllib.TOMLDecodeError, KeyError, TypeError, AttributeError):
        return None


def save_layout(name: str, root: Node) -> str:
    layouts_dir().mkdir(parents=True, exist_ok=True)
    f = layouts_dir() / f"{name.lower()}.toml"
    f.write_text(f"# Glimpse Terminal launchpad. LP {name.upper()} loads it.\nname = {config._value(name.upper())}\n"
                 f"layout = {config._value(to_doc(root))}\n")
    return str(f)


# ── the layout ──────────────────────────────────────────────

class TileLayout(Layout):
    name = "tile"

    def arrange(self, parent: Widget, children: list[Widget], size: Size, greedy: bool = True) -> ArrangeResult:
        ws: Workspace = parent  # type: ignore[assignment]
        place = {id(lf.pane): r for lf, r in rects(ws.root, 0, 0, size.width, size.height)} if ws.root else {}
        if ws.zoomed and ws.focused:
            place = {id(ws.focused.pane): Region(0, 0, size.width, size.height)}
        none = Region(0, 0, 0, 0)
        return [WidgetPlacement(place.get(id(w), none), Offset(0, 0), Spacing(), w, 0, False) for w in children]


class Workspace(Widget):
    """The tiled area. Holds the tree; the app owns what the commands mean."""

    DEFAULT_CSS = "Workspace { height: 1fr; width: 1fr; }"

    def __init__(self, id: str = "term") -> None:
        super().__init__(id=id)
        self.root: Node | None = None
        self.focused: Leaf | None = None
        self.zoomed = False
        self.name_ = ""
        self._tile = TileLayout()

    @property
    def layout(self) -> Layout:          # Textual asks the widget for its layout; ours is the tree
        return self._tile

    @property
    def panes(self) -> list[FuncPane]:
        return [lf.pane for lf in leaves(self.root) if lf.pane] if self.root else []

    @property
    def pane(self) -> FuncPane | None:
        return self.focused.pane if self.focused else None

    def number(self, leaf: Leaf) -> int:
        return leaves(self.root).index(leaf) + 1 if self.root else 0

    def relayout(self) -> None:
        for i, lf in enumerate(leaves(self.root) if self.root else [], 1):
            if lf.pane:
                lf.pane.number, lf.pane.active = i, lf is self.focused
        self._arrangement_cache.clear()     # Textual caches on (size, children); the tree changed under the same children
        self.refresh(layout=True)

    def focus_leaf(self, leaf: Leaf | None) -> None:
        if leaf:
            self.focused = leaf
            self.relayout()

    def cycle(self, d: int = 1) -> None:
        ls = leaves(self.root) if self.root else []
        if ls:
            self.focus_leaf(ls[(ls.index(self.focused) + d) % len(ls)] if self.focused in ls else ls[0])

    def move(self, direction: str) -> None:
        """ctrl-w h j k l: the nearest pane whose rectangle lies that way."""
        if not (self.root and self.focused):
            return
        rs = rects(self.root, 0, 0, max(self.size.width, 1), max(self.size.height, 1))
        here = next(r for lf, r in rs if lf is self.focused)
        cx, cy = here.x + here.width / 2, here.y + here.height / 2
        best, best_d = None, 1e9
        for lf, r in rs:
            if lf is self.focused:
                continue
            ox, oy = r.x + r.width / 2, r.y + r.height / 2
            ahead = {"h": r.x + r.width <= here.x, "l": r.x >= here.x + here.width,
                     "k": r.y + r.height <= here.y, "j": r.y >= here.y + here.height}[direction]
            overlap = (r.y < here.y + here.height and here.y < r.y + r.height) if direction in "hl" else (
                r.x < here.x + here.width and here.x < r.x + r.width)
            d = abs(ox - cx) + abs(oy - cy) + (0 if overlap else 1000)
            if ahead and d < best_d:
                best, best_d = lf, d
        self.focus_leaf(best)


# ── the base of every function page ─────────────────────────

class FuncPane(Widget):
    """One function against one security. Subclasses set `code`, fetch in `load`, and return lines from `draw`.

    Rules the base keeps for them: `load` runs off the paint path and never raises into the app; a failed load
    keeps the last picture and shows the error in the frame; `render` only ever reads cached state (rule 6).
    """

    code: ClassVar[str] = "?"
    every: ClassVar[float] = 60.0           # seconds between loads; 0 loads once
    selectable: ClassVar[bool] = False      # j k move a row cursor instead of scrolling
    tick: ClassVar[float] = 5.0             # redraw at least this often, so ages stay honest

    DEFAULT_CSS = "FuncPane { background: #0D0D0D; }"

    def __init__(self, hub: Hub, security: Instrument | None = None, args: Sequence[str] = ()) -> None:
        super().__init__()
        self.hub, self.security, self.args = hub, security, tuple(args)
        self.securities: tuple[Instrument, ...] = (security,) if security else ()
        self.number, self.active = 0, False
        self.title = ""                                 # after the chip: "BTC · 1H", "mempool"
        self.provs: list[Provenance] = []               # every source behind what is on screen
        self.error = ""
        self.loaded_at = 0.0
        self.fetching = False
        self.cur, self.top, self.n_rows = 0, 0, 0       # row cursor, first visible line, selectable rows
        self.version = 0
        self._cache: tuple[tuple, Text] | None = None

    @classmethod
    def from_command(cls, hub: Hub, cmd: Any) -> FuncPane:
        """Build the pane for a parsed GO bar command. `GP BTC XAU SPX` keeps every security."""
        pane = cls(hub, cmd.security, cmd.args)
        pane.securities = tuple(cmd.securities)
        return pane

    # for subclasses ─────────────────────────────────────────

    async def load(self) -> None:
        """Fetch through self.hub and store the results on self. Raise SourceError when nothing could be shown."""

    def draw(self, w: int, h: int) -> list[Text]:
        """The page body as lines, `w` wide. More than `h` lines scroll."""
        return []

    def key(self, k: str, ch: str | None) -> bool:
        """A key the base did not use. True when handled."""
        return False

    def enter(self) -> str | None:
        """Enter on the cursor row: return a GO bar command to run, or None."""
        return None

    def menu(self) -> list[tuple[str, str]]:
        """Numbered items, picked with the digits: (label, GO bar command)."""
        return []

    def export(self) -> tuple[list[str], list[list[Any]]] | None:
        """(header, rows) for EXP, or None when the page has no table."""
        return None

    def command(self) -> str:
        """The GO bar text that reopens this pane, written into a saved launchpad."""
        return " ".join([self.code, *(s.ticker for s in self.securities), *self.args])

    def cache_key(self) -> tuple:
        return ()

    # loading ────────────────────────────────────────────────

    async def reload(self) -> None:
        if self.fetching:
            return
        self.fetching = True
        try:
            await self.load()
            self.error = ""
        except SourceError as e:
            self.error = str(e)
        except Exception as e:                          # a page bug must not take the terminal down; show it in the frame
            self.error = f"{type(e).__name__}: {e}"[:120]
        finally:
            self.fetching, self.loaded_at = False, time.time()
            self.bump()

    def due(self, now: float) -> bool:
        return not self.fetching and (self.loaded_at == 0 or (self.every > 0 and now - self.loaded_at >= self.every))

    def bump(self) -> None:
        self.version += 1
        if self.is_attached:
            try:
                self.refresh()
            except Exception:           # a load that finishes while the app is closing has no screen to paint on
                pass

    # keys the base owns ─────────────────────────────────────

    def on_key_(self, k: str, ch: str | None, n: int = 1) -> bool:
        page = max(self.size.height - 4, 1)
        step = {"j": n, "down": n, "k": -n, "up": -n, "ctrl+d": page // 2, "ctrl+u": -(page // 2), "ctrl+f": page,
                "pagedown": page, "ctrl+b": -page, "pageup": -page}.get(k)
        if ch == "G":
            step = 10**9
        if step is not None:
            if self.selectable:
                self.cur = max(0, min(self.cur + step, max(self.n_rows - 1, 0)))
            else:
                self.top = max(0, self.top + step)
            self.bump()
            return True
        if self.key(k, ch):
            self.bump()
            return True
        return False

    def home(self) -> None:
        self.cur = self.top = 0
        self.bump()

    # drawing ────────────────────────────────────────────────

    def frame_top(self, w: int, now: float) -> Text:
        edge = ORANGE if self.active else RULE
        out = Text(no_wrap=True, overflow="crop")
        out.append("┌", style=edge)
        out.append(f"{self.number} ", style=f"bold {ORANGE if self.active else DIM}")
        out.append(f" {self.code} ", style=f"bold {INK} on {ORANGE}" if self.active else f"bold {ORANGE} on #2b1500")
        if self.title:
            out.append(f" {self.title} ", style=f"bold {TEXT}")
        right = Text(no_wrap=True)
        if self.error:
            right.append(f" {self.error[:max(w - out.cell_len - 8, 8)]} ", style=f"bold {RED}")
        elif self.provs:
            names = list(dict.fromkeys(p.source.split(":")[0] for p in self.provs))
            delays = list(dict.fromkeys(p.delay for p in self.provs))
            stale = [p for p in self.provs if p.stale(now)]
            label = " + ".join(names[:3]) + ("…" if len(names) > 3 else "") + " · " + "/".join(delays[:2])
            if stale:
                right.append(f" {label} · stale {ago(max(p.age(now) for p in stale))} ", style=f"bold {RED}")
            else:
                right.append(f" {label} ", style=DIM)
        elif self.fetching or not self.loaded_at:
            right.append(" loading ", style=FAINT)
        room = w - out.cell_len - right.cell_len - 1
        if room < 1 and self.provs and not self.error:      # too narrow for the names: keep the delay, and above all keep "stale"
            stale = [p for p in self.provs if p.stale(now)]
            short = f" stale {ago(max(p.age(now) for p in stale))} " if stale else f" {self.provs[0].delay} "
            right = Text(short, style=f"bold {RED}" if stale else DIM)
            room = w - out.cell_len - right.cell_len - 1
        if room < 1:
            right = Text("")
            room = w - out.cell_len - 1
        out.append("─" * max(room, 0), style=edge)
        out.append_text(right)
        out.append("┐", style=edge)
        return out

    def frame_bottom(self, w: int, hint: str = "") -> Text:
        edge = ORANGE if self.active else RULE
        out = Text(no_wrap=True, overflow="crop")
        out.append("└", style=edge)
        hint = f" {hint} " if hint and len(hint) + 6 < w else ""
        out.append("─" * max(w - 2 - len(hint) - 1, 0), style=edge)
        out.append(hint, style=FAINT)
        out.append("─┘" if w >= 3 else "┘", style=edge)
        return out

    def hint(self) -> str:
        """Keys for the bottom edge of the frame."""
        return ""

    def render(self) -> Text:
        w, h = self.size.width, self.size.height
        if w < 8 or h < 3:
            return Text("")
        now = time.time()
        stale = any(p.stale(now) for p in self.provs)
        key = (w, h, self.version, self.cur, self.top, self.active, self.number, stale, int(now // self.tick) if self.tick else 0,
               self.cache_key())
        if self._cache and self._cache[0] == key:
            return self._cache[1]
        inner_w, inner_h = w - 4, h - 2
        try:
            lines = self.draw(inner_w, inner_h)
        except Exception as e:                           # never let one page's drawing bug blank the terminal
            lines = ui.centre([f"{self.code} could not draw: {type(e).__name__}: {e}"[:inner_w]], inner_w, inner_h, RED)
        if not lines and not self.loaded_at:
            lines = ui.centre([f"loading {self.code}…"], inner_w, inner_h, FAINT)
        elif not lines and self.error:
            lines = ui.centre([self.error[:inner_w], "", "The last good value returns when the source does. SRC shows every source."],
                              inner_w, inner_h, DIM)
        self.top = max(0, min(self.top, max(len(lines) - inner_h, 0)))
        shown = lines[self.top:self.top + inner_h]
        edge = ORANGE if self.active else RULE
        more = len(lines) - self.top - inner_h
        out = [self.frame_top(w, now)]
        for ln in shown + [Text("")] * (inner_h - len(shown)):
            row = Text(no_wrap=True, overflow="crop")
            row.append("│ ", style=edge)
            body = ln.copy()
            body.truncate(inner_w)
            body.pad_right(inner_w - body.cell_len)
            if stale:
                body.stylize("dim")                     # the last good value stays, dimmed (rule 2)
            row.append_text(body)
            row.append(" │", style=edge)
            out.append(row)
        scroll = f"{'↑' if self.top else ''}{'↓ ' + str(more) + ' more' if more > 0 else ''}"
        out.append(self.frame_bottom(w, " · ".join(x for x in (self.hint(), scroll) if x)))
        text = Text("\n", no_wrap=True).join(out)
        self._cache = (key, text)
        return text

    def follow(self, row: int, h: int, head: int = 2) -> None:
        """Keep a cursor row on screen: call from draw with the line the cursor is on."""
        if row < self.top + head:
            self.top = max(row - head, 0)
        elif row >= self.top + h:
            self.top = row - h + 1
