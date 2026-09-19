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
