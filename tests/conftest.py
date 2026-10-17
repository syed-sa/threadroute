import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def tr_env(tmp_path, monkeypatch):
    """Every test gets its own data folder, mock providers and zero retry back-off."""
    monkeypatch.setenv("THREADROUTE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("THREADROUTE_LIVE_APIS", "false")
    monkeypatch.setenv("THREADROUTE_MODEL", "scripted")
    for var in ("GOOGLE_API_KEY", "GEMINI_API_KEY", "GOOGLE_MAPS_API_KEY", "CARRIER_API_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    from threadroute.resilience import breaker, store

    breaker.reset_all()
    for component in ("carrier", "maps", "weather", "email", "sheets", "mcp"):
        store.set_config(component, base_delay=0.0)
    yield
    breaker.reset_all()


@pytest.fixture
def scripted():
    from threadroute.testing import ScriptedLlm

    return ScriptedLlm(script=[], seen=[])


@pytest.fixture
async def runtime(scripted):
    from threadroute.agents.runtime import ThreadRouteRuntime

    rt = ThreadRouteRuntime(model=scripted)
    yield rt
    await rt.close()
