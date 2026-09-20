"""The headline feeds and the article reader, on recorded pages."""
from offline import ROOT

from glimpse_tui.data import news


def test_every_feed_parses_newest_first_with_times_and_links():
    for key, outlet, tag, _, _ in news.FEEDS:
        name = {"bbc_world": "bbc_world", "cnbc_world": "cnbc_world", "cnbc_econ": "cnbc_economy", "cnbc_finance": "cnbc_finance",
                "fed_press": "fed", "ecb_press": "ecb", "coindesk": "coindesk"}[key]
        rows = news.parse((ROOT / "news" / f"{name}.xml").read_text(), outlet, tag)
        assert len(rows) >= 10, key
        assert all(r.title and r.link.startswith("https://") and r.at > 0 for r in rows), key
        assert [r.at for r in rows] == sorted((r.at for r in rows), reverse=True), key


def test_the_reader_keeps_the_story_and_drops_the_furniture():
    art = news.read((ROOT / "news" / "cnbc_article.html").read_text(), "https://www.cnbc.com/x", "CNBC")
    assert "consumer sentiment" in art.title.lower()
    body = [t for k, t in art.paragraphs if k == "p"]
    assert len(body) >= 5 and any(t.startswith("Goldman Sachs identified") for t in body)      # key points first, then the story
    assert all(len(t) >= news.MIN_PARAGRAPH for t in body)
    bbc = news.read((ROOT / "news" / "bbc_article.html").read_text(), "https://www.bbc.co.uk/news/articles/c63d7lexyym1o", "BBC")
    assert "Greenland" in bbc.title and len(bbc.paragraphs) >= 10


def test_the_reader_skips_scripts_navigation_and_government_banners():
    html = """<html><head><meta property="og:title" content="Fallback"><script>var x = "a paragraph that is long enough to keep";</script></head>
    <body><nav><p>Home News Markets Business Video and everything else on the menu bar</p></nav>
    <p>Official websites use .gov. A .gov website belongs to an official government organization in the United States.</p>
    <h1>The Board announces a decision</h1><p>Short.</p>
    <p>The Federal Reserve Board on Friday announced the termination of the enforcement action listed below.</p>
    <h2>Background</h2><p>The action was taken in 2024 and the bank has since met every requirement that it set out.</p>
    <p>The Federal Reserve Board on Friday announced the termination of the enforcement action listed below.</p>
    <h2>Related</h2><footer><p>Copyright notice and every link to every other page on this site, repeated.</p></footer></body></html>"""
    art = news.read(html, "https://www.federalreserve.gov/x")
    assert art.title == "The Board announces a decision" and art.outlet == "www.federalreserve.gov"
    assert art.paragraphs == [("p", "The Federal Reserve Board on Friday announced the termination of the enforcement action listed below."),
                              ("h", "Background"),
                              ("p", "The action was taken in 2024 and the bank has since met every requirement that it set out.")]
