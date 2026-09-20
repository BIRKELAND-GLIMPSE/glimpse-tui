"""NEWS: what is happening in the world, the economy, the markets, at the Fed and the ECB, and in crypto, newest first.

One list from seven public feeds (data/news.py), each headline tagged by kind. `[ ]` narrows it to one kind. Enter
opens the story in the same window as `READ`, its text in the terminal; esc or backspace comes back to the list.
"""
from __future__ import annotations

import asyncio
import textwrap
import time

from rich.text import Text

from ..charts import BLUE, CYAN, VIOLET
from ..data import news
from ..data.core import SourceError
from ..term import ui
from ..term.panes import FuncPane
from ..term.registry import Function, register
from ..theme import DIM, FAINT, GREEN, ORANGE, TEXT

KEEP_S = 3 * 86400          # older headlines drop off
MAX_ROWS = 120
PER_FEED = 15
TAG_COLOUR = {"WORLD": BLUE, "ECONOMY": GREEN, "MARKETS": CYAN, "FED": VIOLET, "ECB": VIOLET, "CRYPTO": ORANGE}
FILTERS = ("ALL", *news.TAGS)


class NewsPane(FuncPane):
    code, every, selectable, tick = "NEWS", 300, True, 30.0

    def __init__(self, hub, security=None, args=()) -> None:
        super().__init__(hub, security, args)
        want = next((a.upper() for a in self.args if a.upper() in FILTERS), "ALL")
        self.filter = want
        self.items: list[news.Headline] = []
        self.failed: list[str] = []
        self.title = self._title()

    def _title(self) -> str:
        return "world · economy · markets · central banks · crypto" if self.filter == "ALL" else self.filter.lower()

    def _feeds(self) -> list[news.Feed]:
        out = [s for s in self.hub.sources.values() if isinstance(s, news.Feed)]
        return [f for f in out if self.filter in ("ALL", f.tag)]

    async def load(self) -> None:
        feeds = self._feeds()
        got = await asyncio.gather(*(f.headlines() for f in feeds), return_exceptions=True)
        rows, provs, failed = [], [], []
        for f, g in zip(feeds, got, strict=True):
            if isinstance(g, BaseException):
                failed.append(f.outlet)
                continue
            rows += g[0][:PER_FEED]                     # no one outlet drowns out the rest
            provs.append(g[1])
        now, seen, keep = time.time(), set(), []
        for h in sorted(rows, key=lambda h: -h.at):
            key = h.title.lower()
            if key in seen or (h.at and now - h.at > KEEP_S):
                continue
            seen.add(key)
            keep.append(h)
        self.items, self.provs, self.failed = keep[:MAX_ROWS], provs, failed
        self.n_rows = len(self.items)                   # the cursor can move before the list is first drawn
        self.title = self._title()
        if not keep and failed:
            raise SourceError("no news feed answered: " + ", ".join(failed))

    def cache_key(self) -> tuple:
        return (self.filter, len(self.items), self.items[0].title if self.items else "")

    def draw(self, w: int, h: int) -> list[Text]:
        now = time.time()
        self.n_rows = len(self.items)
        self.cur = min(self.cur, max(self.n_rows - 1, 0))
        tag_w = max(len(t) for t in news.TAGS)
        show_outlet = w >= 78
        out: list[Text] = []
        for i, it in enumerate(self.items):
            line = Text(no_wrap=True, overflow="ellipsis")
            line.append(f"{ui.when(it.at, now) if it.at else '':>3}  ", style=FAINT)
            line.append(f"{it.tag:<{tag_w}}  ", style=f"bold {TAG_COLOUR.get(it.tag, DIM)}")
            tail = f"  {it.outlet}" if show_outlet else ""
            room = w - line.cell_len - len(tail)
            title = it.title if len(it.title) <= room else it.title[:max(room - 1, 0)] + "…"
            line.append(title, style=f"bold {TEXT}" if i == self.cur and self.active else TEXT)
            if tail:
                line.append(" " * max(room - len(title), 0) + tail, style=FAINT)
            if i == self.cur and self.active:
                line.pad_right(max(w - line.cell_len, 0))
                line.stylize(f"on {ui.CURSOR_BG}")
            out.append(line)
        foot: list[Text] = []
        if self.failed:
            foot.append(ui.note("not answering: " + ", ".join(self.failed), FAINT))
        if not out and self.loaded_at:
            out = [ui.note("No headlines in the last three days from these feeds.", DIM)]
        room = max(h - len(foot), 1)
        self.follow(self.cur, room, head=0)
        if foot and len(out) < room:
            out += [Text("")] * (h - len(out) - len(foot))
        return out + foot

    def key(self, k: str, ch: str | None) -> bool:
        if ch in ("[", "]"):
            i = FILTERS.index(self.filter)
            self.filter = FILTERS[(i + (1 if ch == "]" else -1)) % len(FILTERS)]
            self.cur = self.top = 0
            self.title = self._title()
            self.loaded_at = 0.0                        # the next tick loads the narrower list
            return True
        return False

    def enter(self) -> str | None:
        if 0 <= self.cur < len(self.items) and (link := self.items[self.cur].link):
            return f"READ {link}"
        return None

    def hint(self) -> str:
        return f"{self.filter.lower()} · j k choose · enter or l read · [ ] topic"

    def menu(self) -> list[tuple[str, str]]:
        return [(t, f"NEWS {t}") for t in FILTERS[:9]]

    def export(self):
        from datetime import UTC, datetime
        return (["published_utc", "tag", "outlet", "title", "link"],
                [[datetime.fromtimestamp(h.at, UTC).strftime("%Y-%m-%d %H:%M") if h.at else "", h.tag, h.outlet, h.title, h.link]
                 for h in self.items])


class ReadPane(FuncPane):
    """READ <url>: one story's text, in the terminal. esc or backspace returns to the list it came from."""
    code, every, tick = "READ", 0, 60.0

    def __init__(self, hub, security=None, args=()) -> None:
        super().__init__(hub, security, args)
        self.url = self.args[0] if self.args else ""
        self.article: news.Article | None = None
        self.title = "article"

    async def load(self) -> None:
        if not self.url.startswith("https://"):
            raise SourceError("READ takes a story's address: READ https://…")
        src = news.pages(self.hub, self.url)
        self.article, prov = await src.article(self.url, src.name)
        self.title = self.article.outlet
        self.provs = [prov]

    def cache_key(self) -> tuple:
        return (id(self.article),)

    def draw(self, w: int, h: int) -> list[Text]:
        a = self.article
        if a is None:
            return []
        width = min(w, 96)                                  # a comfortable line length, however wide the window
        out = [Text(line, style=f"bold {TEXT}") for line in textwrap.wrap(a.title, width)] + [ui.note(a.outlet, FAINT), Text("")]
        for kind, text in a.paragraphs:
            if kind == "h":
                out += [Text(line, style=f"bold {ORANGE}") for line in textwrap.wrap(text, width)]
            else:
                out += [Text(line, style=DIM) for line in textwrap.wrap(text, width)] + [Text("")]
        return out

    def hint(self) -> str:
        return "j k scroll · n p next / previous story · h esc back to the list"

    def command(self) -> str:
        return f"READ {self.url}"


register(Function(
    "READ", "Read", "Markets", "a news story's text, read in the terminal", ReadPane, args="<url>", needs=("articles",),
    help="The story behind a NEWS headline, as text: its headline, then its paragraphs and subheadings, wrapped to a comfortable "
         "width. The page is fetched once from the outlet when you open it (BBC, CNBC, the Federal Reserve, the ECB, CoinDesk), "
         "read the way a text browser reads it, and kept for half an hour. Photos, video, share buttons and adverts are left out. "
         "j and k scroll. esc or backspace goes back to the headlines. A page with no article text, such as a video, says so."))


register(Function(
    "NEWS", "News", "Markets", "the headlines that matter: world, economy, markets, the Fed and the ECB, crypto", NewsPane,
    args=f"[{' | '.join(FILTERS)}]", needs=("headlines",),
    help="One list, newest first, from public RSS feeds: BBC World, CNBC International, CNBC Economy, CNBC Finance, the Federal "
         "Reserve's press releases, the ECB's press releases, and CoinDesk. Each row is how long ago it was published, what kind of "
         "story it is (WORLD, ECONOMY, MARKETS, FED, ECB, CRYPTO), the headline and the outlet. The fifteen newest from each feed "
         "are kept, so no one outlet crowds out the rest. [ and ] narrow the list to one kind, or NEWS FED opens it that way. "
         "Enter opens the story right here, as text (READ), and esc or backspace comes back. The same story from two feeds shows "
         "once. Headlines older than three days drop off. Each feed is asked at most every five minutes. A feed that does not answer "
         "is named at the foot and the rest carry on."))
