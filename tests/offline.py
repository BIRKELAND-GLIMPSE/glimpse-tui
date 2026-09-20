"""The recorded internet. Every request the suite makes is answered from tests/fixtures/sources/, or fails.

Routes come from each source's `_manifest.json` (file -> the URL it was recorded from) plus the table below for
sources recorded without one. A request with no fixture gets a 599 and is written down, so a test can assert that
nothing unexpected was asked for.
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import httpx

ROOT = Path(__file__).parent / "fixtures" / "sources"

# (host, path) -> fixture, for sources recorded without a manifest. A `{}` in the path matches one segment or query value.
EXTRA: dict[tuple[str, str], str] = {
    ("api.exchange.coinbase.com", "/products/BTC-USD/ticker"): "coinbase/ticker.json",
    ("api.exchange.coinbase.com", "/products/BTC-USD/stats"): "coinbase/stats.json",
    ("api.exchange.coinbase.com", "/products/BTC-USD/candles"): "coinbase/candles_btcusd_1d.json",
    ("api.exchange.coinbase.com", "/products/ETH-USD/ticker"): "coinbase/ticker_ETH-USD.json",
    ("api.exchange.coinbase.com", "/products/ETH-USD/stats"): "coinbase/stats.json",
    ("www.bitstamp.net", "/api/v2/ticker/btcusd/"): "bitstamp/ticker_btcusd.json",
    ("api.kraken.com", "/0/public/OHLC"): "kraken/ohlc_xbtusd_1440.json",
    ("api.exchange.coinbase.com", "/products/PAXG-USD/candles"): "coinbase/candles_paxgusd_1d.json",
    ("www.deribit.com", "/api/v2/public/get_volatility_index_data"): "deribit/dvol_btc_1d.json",
    ("www.deribit.com", "/api/v2/public/get_book_summary_by_currency"): "deribit/book_summary_btc_option.json",
    ("www.deribit.com", "/api/v2/public/get_index_price"): "deribit/index_price_btc_usd.json",
    ("www.deribit.com", "/api/v2/public/get_instruments"): "deribit/instruments_btc_option.json",
    ("api.alternative.me", "/fng/"): "alternative_me/fng_limit30.json",
    ("www.ecb.europa.eu", "/stats/eurofxref/eurofxref-daily.xml"): "ecb/eurofxref-daily.xml",
    ("www.ecb.europa.eu", "/stats/eurofxref/eurofxref-hist-90d.xml"): "ecb/eurofxref-hist-90d.xml",
    ("feeds.bbci.co.uk", "/news/world/rss.xml"): "news/bbc_world.xml",
    ("www.cnbc.com", "/id/20910258/device/rss/rss.html"): "news/cnbc_economy.xml",
    ("www.cnbc.com", "/id/10000664/device/rss/rss.html"): "news/cnbc_finance.xml",
    ("www.cnbc.com", "/id/100727362/device/rss/rss.html"): "news/cnbc_world.xml",
    ("www.federalreserve.gov", "/feeds/press_all.xml"): "news/fed.xml",
    ("www.ecb.europa.eu", "/rss/press.html"): "news/ecb.xml",
    ("www.coindesk.com", "/arc/outboundfeeds/rss"): "news/coindesk.xml",
}


def _routes() -> dict[str, Path]:
    out: dict[str, Path] = {}
    for manifest in ROOT.glob("*/_manifest.json"):
        try:
            files = json.loads(manifest.read_text()).get("files", {})
        except ValueError:
            continue
        for name, meta in files.items():
            if isinstance(meta, dict) and meta.get("url") and meta.get("status", 200) == 200 and (manifest.parent / name).is_file():
                out.setdefault(meta["url"], manifest.parent / name)
    return out


class Recorded:
    """An httpx handler. `missing` lists every URL that had no fixture; `seen` every URL asked for."""

    def __init__(self) -> None:
        self.by_url = _routes()
        self.by_path: dict[tuple[str, str], Path] = {}
        for url, f in self.by_url.items():
            u = urlsplit(url)
            self.by_path.setdefault((u.netloc, u.path), f)
        for key, rel in EXTRA.items():
            if (ROOT / rel).is_file():
                self.by_path[key] = ROOT / rel
        self.seen: list[str] = []
        self.missing: list[str] = []
        self.fail: set[str] = set()                     # hosts that answer 503, to test the offline path
        self.limited: set[str] = set()                  # hosts that answer 429

    def _kraken(self, u) -> Path | None:
        pairs = dict(parse_qsl(u.query)).get("pair", "")
        if u.path == "/0/public/Ticker":
            if "x" in pairs.replace("XBT", "").replace("XX", ""):
                name = "ticker_xstocks.json" if any(p.endswith("xUSD") for p in pairs.split(",")) and "XBT" not in pairs else "ticker_fx.json"
                return ROOT / "kraken" / name
            return ROOT / "kraken" / ("ticker_xbtusd.json" if pairs in ("XBTUSD", "XXBTZUSD") else "ticker_fx.json")
        return None

    def _fred(self, u) -> Path | None:
        sid = dict(parse_qsl(u.query)).get("id", "")
        f = ROOT / "fred" / f"{sid}.csv"
        return f if f.is_file() else None

    def _treasury(self, u) -> Path | None:
        q = dict(parse_qsl(u.query))
        kind = {"daily_treasury_yield_curve": "yield_curve", "daily_treasury_real_yield_curve": "real_yield_curve",
                "daily_treasury_bill_rates": "bill_rates"}.get(q.get("data", ""), "")
        if not kind:
            return None
        if q.get("field_tdr_date_value_month"):
            f = ROOT / "treasury" / f"{kind}_202609.xml"
        else:
            f = ROOT / "treasury" / f"{kind}_{q.get('field_tdr_date_value', '2026')}.xml"      # 2025 for the curve a year ago
            f = f if f.is_file() else ROOT / "treasury" / f"{kind}_2026.xml"
        return f if f.is_file() else None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.seen.append(url)
        u = urlsplit(url)
        if u.netloc in self.fail:
            return httpx.Response(503, text="down for the test")
        if u.netloc in self.limited:
            return httpx.Response(429, headers={"retry-after": "120"}, text="slow down")
        f = self.by_url.get(url) or self.by_url.get(url.replace("%2C", ","))     # httpx encodes the commas of a bulk request
        if u.netloc == "api.kraken.com" and u.path == "/0/public/Ticker":
            # One request names many pairs. Answer with the recorded rows for the pairs asked for, from whichever
            # recorded Ticker response holds each one. Values are untouched.
            tokens = dict(parse_qsl(u.query)).get("asset_class") == "tokenized_asset"
            merged: dict = {}
            for name in (("ticker_xstocks.json",) if tokens else ("ticker_xbtusd.json", "ticker_fx.json")):
                merged.update(json.loads((ROOT / "kraken" / name).read_text())["result"])
            return httpx.Response(200, json={"error": [], "result": merged})
        if u.netloc == "query1.finance.yahoo.com" and u.path == "/v7/finance/spark":
            # One request names up to 20 symbols: answer with the recorded rows for those symbols. Values are untouched.
            want = set(dict(parse_qsl(u.query)).get("symbols", "").split(","))
            doc = json.loads((ROOT / "yahoo" / "spark_all.json").read_text())
            rows = [r for r in doc["spark"]["result"] if r["symbol"] in want]
            return httpx.Response(200, json={"spark": {"result": rows, "error": None}})
        if f is None and u.netloc == "query1.finance.yahoo.com" and u.path.startswith("/v8/finance/chart/"):
            f = ROOT / "yahoo" / "chart_gspc_2y.json"           # any symbol's history answers with the S&P 500's
        if f is None and u.netloc == "api.exchange.coinbase.com" and u.path.endswith("/ticker"):
            f = ROOT / "coinbase" / f"ticker_{u.path.split('/')[2]}.json"
            f = f if f.is_file() else None
        if f is None and u.netloc == "api.exchange.coinbase.com" and u.path.endswith("/stats"):
            f = ROOT / "coinbase" / "stats.json"
        if f is None and u.netloc == "fred.stlouisfed.org":
            f = self._fred(u)
        if f is None and u.netloc == "home.treasury.gov":
            f = self._treasury(u)
        if f is None:
            f = self.by_path.get((u.netloc, u.path))
        if f is None and u.netloc == "mempool.space" and u.path.startswith("/api/address/"):
            # Any address answers with the one recorded address: enough to drive a gap-limit scan offline.
            tail = u.path.rsplit("/", 1)[-1]
            f = ROOT / "mempool" / {"txs": "address_txs.json", "utxo": "address_utxo.json"}.get(tail, "address.json")
        if f is None:
            self.missing.append(url)
            return httpx.Response(599, text=f"no fixture for {url}")
        body = f.read_bytes()
        kind = "application/json" if f.suffix == ".json" else "text/plain"
        return httpx.Response(200, content=body, headers={"content-type": kind})


def fixture(rel: str):
    """A recorded response as parsed JSON, or text for anything that is not JSON."""
    f = ROOT / rel
    return json.loads(f.read_text()) if f.suffix == ".json" else f.read_text()
