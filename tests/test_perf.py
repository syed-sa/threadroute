import asyncio
import time

import pytest

from threadroute import integrations
from threadroute.agents import tools as T
from threadroute.perf import cache
from threadroute.perf.metrics import metrics
from threadroute.resilience import chaos


@pytest.fixture
def ttl(monkeypatch):
    monkeypatch.setenv("THREADROUTE_CACHE_TTL", "30")
    cache.invalidate()
    yield
    cache.invalidate()


async def test_cache_hits_and_invalidation(ttl):
    before = metrics.counter("cache_hit.carrier")
    a = await integrations.track_shipment("SH-1001")
    b = await integrations.track_shipment("SH-1001")
    assert a == b and metrics.counter("cache_hit.carrier") == before + 1
    chaos.set_rule("carrier:primary_api", "fail")  # changing state must drop cached answers
    c = await integrations.track_shipment("SH-1001")
    assert metrics.counter("cache_hit.carrier") == before + 1 and c.get("degraded") is not None
    chaos.clear()


async def test_degraded_and_error_results_are_never_cached(ttl):
    chaos.set_rule("carrier:primary_api", "fail")
    chaos.set_rule("carrier:secondary_api", "fail")
    first = await integrations.track_shipment("SH-1001")
    assert first.get("degraded")
    chaos.clear()  # also resets breakers/demotions
    again = await integrations.track_shipment("SH-1001")
    assert not again.get("degraded")  # not served from a cached fallback


async def test_cache_disabled_with_ttl_zero(monkeypatch):
    monkeypatch.setenv("THREADROUTE_CACHE_TTL", "0")
    before = metrics.counter("cache_hit.weather")
    await integrations.get_weather_risk("Nagpur Hub")
    await integrations.get_weather_risk("Nagpur Hub")
    assert metrics.counter("cache_hit.weather") == before


async def test_reroute_lookups_run_concurrently(monkeypatch):
    calls = []

    async def slow_route(origin, destination, mode="road"):
        calls.append(mode)
        await asyncio.sleep(0.2)
        return {"ok": True, "distance_km": 500, "duration_hours": 10}

    monkeypatch.setattr(T.integrations, "estimate_route", slow_route)
    t0 = time.perf_counter()
    out = await T.get_reroute_options("SH-1001")
    elapsed = time.perf_counter() - t0
    assert len(calls) >= 3 and out["alternatives"] and elapsed < 0.2 * len(calls) * 0.6  # far below sequential time


def test_agents_share_one_mcp_session_but_keep_their_own_allow_lists(monkeypatch):
    from google.adk.tools.mcp_tool.mcp_toolset import McpToolset

    from threadroute.agents.factory import build_root_agent
    from threadroute.testing import ScriptedLlm

    root = build_root_agent(model=ScriptedLlm(script=[], seen=[]))
    sets = [t for a in root.sub_agents for t in a.tools if isinstance(t, McpToolset)]
    assert len(sets) >= 5 and len({id(t._mcp_session_manager) for t in sets}) == 1
    assert len({tuple(t.tool_filter) for t in sets}) > 1  # allow-lists stay per agent (least privilege)
    monkeypatch.setenv("THREADROUTE_MCP_SHARED", "0")
    root2 = build_root_agent(model=ScriptedLlm(script=[], seen=[]))
    sets2 = [t for a in root2.sub_agents for t in a.tools if isinstance(t, McpToolset)]
    assert len({id(t._mcp_session_manager) for t in sets2}) == len(sets2)
