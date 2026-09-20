import pytest


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """The app remembers the last series in ~/.config/glimpse; tests must neither read nor write the real one."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.delenv("GLIMPSE_COLORS", raising=False)
    monkeypatch.delenv("COLORTERM", raising=False)


@pytest.fixture(autouse=True)
def no_network(tmp_path, monkeypatch):
    """The terminal's data layer can only reach the recorded fixtures. A request with no fixture gets a 599,
    and the WebSocket streams are off: no test touches the network (TERMINAL.md rule 9)."""
    import httpx
    from offline import Recorded

    from glimpse_tui.data import core, http
    from glimpse_tui.term import hub

    rec = Recorded()
    http.use_transport(httpx.MockTransport(rec))
    core.use_disk_cache(None)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(hub.Hub, "streams_default", False, raising=False)
    yield rec
    http.use_transport(None)
    from glimpse_tui import charts
    charts.truecolor = True             # an app started by a test sets the module's palette; the next test starts clean


@pytest.fixture(autouse=True)
def official_sources_first(monkeypatch, request):
    """Most tests pin the official daily path (FRED, the Treasury, the ECB), which is what the terminal falls back to with
    Yahoo off. Tests marked `yahoo` run with Yahoo Finance on, as the terminal ships."""
    from glimpse_tui.term import config
    monkeypatch.setitem(config.DEFAULTS, "sources", {"yahoo": "yahoo" in request.keywords})


@pytest.fixture(autouse=True)
def local_zoo(monkeypatch):
    """The model zoo runs for real in the tests, on a synthetic frame of hourly bars: every bot computes, and
    nothing fetches candles. Without this the swarm would reach Coinbase for its history (TERMINAL.md rule 9)."""
    import time

    import numpy as np
    from zoo_contract import synthetic_bars

    from glimpse_tui import bots
    from glimpse_tui.term import swarm
    from glimpse_tui.zoo import core as C

    class Feed:
        """The candles every bot reads, ending at a price inside the fixture market's ladder."""

        def __init__(self, asset: str) -> None:
            bars = synthetic_bars()
            k = 76_500 / bars["close"].iloc[-1]
            self.asset, self.spot = asset, 76_500.0
            self.bars = bars.assign(**{c: bars[c] * k for c in ("open", "high", "low", "close")})
            self._scale, self._cache = None, {}

        ready = True

        async def refresh(self, max_age: float = 30.0) -> None: ...

        def ctx(self, bins, end, now=None):
            self._scale = self._scale or C.Scale.fit(self.bars)
            edges = np.array([bins[0][0]] + [hi for _, hi in bins], dtype=float)
            return C.Ctx(self.bars, self.spot, edges, time.time() if now is None else now, float(end),
                         asset=self.asset, scale=self._scale, cache=self._cache)

    monkeypatch.setattr(swarm, "make_feed", Feed)
    # A spread of the real zoo rather than all 130: the models are the real ones, a pass is a tenth of the work.
    monkeypatch.setattr(swarm, "catalog", lambda: [b for i, b in enumerate(bots.discover()) if i % 8 == 0])
