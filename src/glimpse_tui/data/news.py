"""Headlines and the articles behind them: the world, the economy, markets, the Fed, the ECB, and crypto.

Each outlet is its own source (its own host, rate budget and health on SRC). The feeds are public RSS 2.0, keyless,
checked live on 19 Sep 2026 with the terminal's own User-Agent. An article is read the way a text browser reads it:
the page is fetched once, when asked for, and its headline and paragraphs are shown. Nothing is stored or passed on.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urlsplit

from .core import DELAYED, Provenance, Source, SourceError

POLL_S = 300.0              # a feed is asked at most once every five minutes

# (source key, outlet, tag, host, path). The tag is the colour-coded label on the NEWS page and what `NEWS <tag>` filters on.
FEEDS = (
    ("bbc_world", "BBC", "WORLD", "https://feeds.bbci.co.uk", "/news/world/rss.xml"),
    ("cnbc_world", "CNBC", "WORLD", "https://www.cnbc.com", "/id/100727362/device/rss/rss.html"),
    ("cnbc_econ", "CNBC", "ECONOMY", "https://www.cnbc.com", "/id/20910258/device/rss/rss.html"),
    ("cnbc_finance", "CNBC", "MARKETS", "https://www.cnbc.com", "/id/10000664/device/rss/rss.html"),
    ("fed_press", "Federal Reserve", "FED", "https://www.federalreserve.gov", "/feeds/press_all.xml"),
    ("ecb_press", "ECB", "ECB", "https://www.ecb.europa.eu", "/rss/press.html"),
    ("coindesk", "CoinDesk", "CRYPTO", "https://www.coindesk.com", "/arc/outboundfeeds/rss"),
)
TAGS = tuple(dict.fromkeys(f[2] for f in FEEDS))


@dataclass(frozen=True)
class Headline:
    title: str
    link: str
    at: float               # unix seconds, from the item's pubDate; 0 when the feed gave none
    outlet: str
    tag: str


def _when(text: str | None) -> float:
    try:
        return parsedate_to_datetime((text or "").strip()).timestamp()
    except (TypeError, ValueError, IndexError):
        return 0.0


def parse(xml: str, outlet: str, tag: str) -> list[Headline]:
    """Every <item> of an RSS 2.0 document, newest first. Items without a title are skipped."""
    try:
        root = ET.fromstring(xml.lstrip("﻿").strip())
    except ET.ParseError as e:
        raise SourceError(f"{outlet}: the feed is not valid XML ({e})") from None
    out = []
    for item in root.iter("item"):
        title = " ".join((item.findtext("title") or "").split())
        if not title:
            continue
        out.append(Headline(title, (item.findtext("link") or "").strip(), _when(item.findtext("pubDate")), outlet, tag))
    return sorted(out, key=lambda h: -h.at)


class Feed(Source):
    def __init__(self, outlet: str, tag: str, base_url: str, path: str) -> None:
        super().__init__(name=outlet, base_url=base_url, delay=DELAYED, rate=0.2, burst=2,
                         serves=f"{tag.lower()} headlines, RSS, polled every {POLL_S / 60:.0f} minutes")
        self.outlet, self.tag, self.path = outlet, tag, path

    async def headlines(self) -> tuple[list[Headline], Provenance]:
        text, at = await self.get(self.path, ttl=POLL_S, text=True)
        rows = parse(text if isinstance(text, str) else "", self.outlet, self.tag)
        return rows, self.prov(at, as_of=rows[0].at if rows and rows[0].at else at)


def build(hub) -> None:
    for key, outlet, tag, base, path in FEEDS:
        hub.add(key, Feed(outlet, tag, base, path))


# ── articles ────────────────────────────────────────────────

SKIP = {"script", "style", "nav", "header", "footer", "aside", "figure", "figcaption", "noscript", "form", "button", "svg", "template"}
BLOCKS = {"p", "h2", "h3", "li"}
BOILERPLATE = (".gov website belongs", "safely connected to the .gov", "share sensitive information only on official", "cookie",
               "sign up for", "subscribe to", "all rights reserved", "follow us on", "download the app")
MIN_PARAGRAPH = 40


@dataclass
class Article:
    url: str
    title: str
    outlet: str
    paragraphs: list[tuple[str, str]] = field(default_factory=list)    # (kind, text): kind is "p" or "h" for a subheading


class _Reader(HTMLParser):
    """Paragraphs and subheadings outside navigation, headers, footers and scripts, in page order."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.depth = 0
        self.kind = ""
        self.buf: list[str] = []
        self.blocks: list[tuple[str, str]] = []
        self.title = ""
        self.h1: list[str] = []
        self.in_h1 = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in SKIP:
            self.skip += 1
        elif tag == "meta" and not self.title:
            a = dict(attrs)
            if a.get("property") == "og:title":
                self.title = (a.get("content") or "").strip()
        elif tag == "h1":
            self.in_h1 = True
        elif tag in BLOCKS and not self.skip:
            self.depth += 1
            if self.depth == 1:
                self.kind, self.buf = ("h" if tag in ("h2", "h3") else "p"), []

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP and self.skip:
            self.skip -= 1
        elif tag == "h1":
            self.in_h1 = False
        elif tag in BLOCKS and self.depth:
            self.depth -= 1
            if self.depth == 0:
                text = " ".join("".join(self.buf).split())
                if text:
                    self.blocks.append((self.kind, text))

    def handle_data(self, data: str) -> None:
        if self.in_h1 and not self.skip:
            self.h1.append(data)
        if self.depth and not self.skip:
            self.buf.append(data)


def read(html: str, url: str = "", outlet: str = "") -> Article:
    """The readable text of a news page: its headline, then paragraphs and subheadings. Short fragments (captions,
    bylines, share buttons) and known boilerplate are dropped, and a paragraph repeated on the page is kept once."""
    r = _Reader()
    r.feed(html)
    title = " ".join("".join(r.h1).split()) or r.title
    seen, out = set(), []
    for kind, text in r.blocks:
        low = text.lower()
        if text in seen or any(b in low for b in BOILERPLATE):
            continue
        if kind == "p" and len(text) < MIN_PARAGRAPH:
            continue
        if kind == "h" and (len(text) > 120 or low == title.lower()):
            continue
        seen.add(text)
        out.append((kind, text))
    while out and out[-1][0] == "h":
        out.pop()                                   # a trailing "More on this story" heading with nothing under it
    return Article(url, title, outlet or urlsplit(url).netloc, out)


class Pages(Source):
    """Article pages on one outlet's site. Asked for one at a time, when the reader opens a story."""

    def __init__(self, host: str, outlet: str) -> None:
        super().__init__(name=outlet, base_url=f"https://{host}", delay=DELAYED, rate=0.5, burst=2, serves=f"{outlet} articles, on request")

    async def article(self, url: str, outlet: str) -> tuple[Article, Provenance]:
        parts = urlsplit(url)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        html, at = await self.get(path, ttl=1800, text=True)
        art = read(html if isinstance(html, str) else "", url, outlet)
        if len(art.paragraphs) < 2:
            raise SourceError(f"{outlet}: this page has no article text the terminal can show")
        return art, self.prov(at)


OUTLETS = {"www.bbc.co.uk": "BBC", "www.bbc.com": "BBC", "www.cnbc.com": "CNBC", "www.federalreserve.gov": "Federal Reserve",
           "www.ecb.europa.eu": "ECB", "www.coindesk.com": "CoinDesk"}


def pages(hub, url: str) -> Pages:
    """The article source for a story's host, made the first time it is needed and kept on the hub."""
    host = urlsplit(url).netloc.lower()
    key = f"pages:{host}"
    if key not in hub.sources:
        hub.add(key, Pages(host, OUTLETS.get(host, host)))
    return hub.sources[key]
