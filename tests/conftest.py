import pytest


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """The app remembers the last series in ~/.config/glimpse; tests must neither read nor write the real one."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.delenv("GLIMPSE_COLORS", raising=False)
    monkeypatch.delenv("COLORTERM", raising=False)
