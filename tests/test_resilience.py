import asyncio

import pytest
from pydantic import BaseModel

from threadroute.resilience import breaker, chaos, store, watchdog
from threadroute.resilience.resilient import AllProvidersFailed, Provider, effective_order, resilient_call


class Out(BaseModel):
    value: int


def flaky(fail_times):
    state = {"n": 0}

    async def fn():
        state["n"] += 1
        if state["n"] <= fail_times:
            raise ConnectionError("boom")
        return {"value": 1}

    fn.state = state
    return fn


async def test_retry_then_succeed_on_same_provider():
    f = flaky(2)
    res = await resilient_call("t", [Provider("p", f)], schema=Out)
    assert res["value"] == 1 and res["source"] == "p" and f.state["n"] == 3
    assert any(e["kind"] == "recovered" for e in store.recent_events())


async def test_falls_back_to_next_provider_and_flags_degraded():
    async def good():
        return {"value": 2}

    res = await resilient_call("t", [Provider("a", flaky(99)), Provider("b", good, degraded=True)], schema=Out)
    assert res["source"] == "b" and res["degraded"] is True and "healing_note" in res
    kinds = [e["kind"] for e in store.recent_events()]
    assert "retry" in kinds and "fallback" in kinds


async def test_all_providers_failed_raises_with_every_error():
    with pytest.raises(AllProvidersFailed) as exc:
        await resilient_call("t", [Provider("a", flaky(99)), Provider("b", flaky(99))])
    assert len(exc.value.errors) >= 2


async def test_open_breaker_skips_provider_without_calling_it():
    a = flaky(99)

    async def good():
        return {"value": 3}

    b = breaker.get_breaker("t:a")
    for _ in range(3):
        b.record_failure()
    assert b.state == "open"
    res = await resilient_call("t", [Provider("a", a), Provider("b", good)], schema=Out)
    assert res["source"] == "b" and a.state["n"] == 0
    assert any(e["kind"] == "breaker_skip" for e in store.recent_events())


async def test_repeated_failures_open_breaker_and_trigger_watchdog_demotion():
    store.set_config("t", max_retries=0)

    async def good():
        return {"value": 3}

    a = flaky(99)
    for _ in range(3):
        await resilient_call("t", [Provider("a", a), Provider("b", good)], schema=Out)
    assert breaker.get_breaker("t:a").state == "open"
    assert "a" in store.get_config("t").active_demotions()
    calls_before = a.state["n"]
    res = await resilient_call("t", [Provider("a", a), Provider("b", good)], schema=Out)
    assert res["source"] == "b" and a.state["n"] == calls_before


async def test_breaker_half_open_recovers(monkeypatch):
    from threadroute.resilience import breaker as bmod

    now = {"t": 1000.0}
    monkeypatch.setattr(bmod, "_clock", lambda: now["t"])
    b = bmod.CircuitBreaker("x", failure_threshold=2, recovery_seconds=10)
    b.record_failure(); b.record_failure()
    assert b.state == "open" and not b.allow()
    now["t"] += 11
    assert b.state == "half_open" and b.allow()
    b.record_success()
    assert b.state == "closed"


async def test_timeout_is_healed():
    async def slow():
        await asyncio.sleep(1)
        return {"value": 1}

    async def good():
        return {"value": 9}

    store.set_config("t", timeout=0.05, max_retries=0)
    res = await resilient_call("t", [Provider("slow", slow), Provider("fast", good)], schema=Out)
    assert res["source"] == "fast"
    assert any(e["kind"] == "timeout" for e in store.recent_events())


async def test_malformed_payload_is_caught_by_schema_and_healed():
    async def fn():
        return {"value": 1}

    async def backup():
        return {"value": 2}

    chaos.set_rule("t:a", "malformed")
    res = await resilient_call("t", [Provider("a", fn), Provider("b", backup)], schema=Out)
    assert res["source"] == "b"
    assert any(e["kind"] == "schema" for e in store.recent_events())


async def test_chaos_count_limits_injection_and_retry_heals_it():
    chaos.set_rule("t:a", "fail", count=1)
    store.set_config("t", max_retries=1)

    async def fn():
        return {"value": 1}

    res = await resilient_call("t", [Provider("a", fn)], schema=Out)
    assert res["source"] == "a" and res["degraded"] is False
    kinds = [e["kind"] for e in store.recent_events()]
    assert "failure" in kinds and "retry" in kinds and "recovered" in kinds
    assert chaos.list_rules()[0]["remaining"] == 0


async def test_chaos_clear_restores_demoted_providers():
    async def fn():
        return {"value": 1}

    async def backup():
        return {"value": 2}

    chaos.set_rule("t:a", "fail")
    for _ in range(3):
        await resilient_call("t", [Provider("a", fn), Provider("b", backup)], schema=Out)
    assert "a" in store.get_config("t").active_demotions()
    order = [p.name for p in effective_order("t", [Provider("a", fn), Provider("b", backup)])]
    assert order == ["b", "a"]
    chaos.clear()
    assert store.get_config("t").active_demotions() == []


def test_watchdog_diagnose_and_apply():
    for _ in range(4):
        store.record_call("carrier:primary_api", False, 10, "ConnectionError: boom")
    recs = watchdog.diagnose()
    assert recs and recs[0]["action"] == "demote_provider" and recs[0]["provider"] == "primary_api"
    applied = watchdog.auto_heal("carrier")
    assert applied[0]["result"]["ok"]
    assert "primary_api" in store.get_config("carrier").active_demotions()
    assert watchdog.apply_remedy("carrier", "restore_provider", "primary_api")["ok"]
    assert store.get_config("carrier").active_demotions() == []


def test_watchdog_timeout_and_retry_remedies():
    for _ in range(4):
        store.record_call("maps:google_routes", True, 300)
    for _ in range(2):
        store.record_call("maps:google_routes", False, 8000, "timeout after 8.0s")
    rec = watchdog.diagnose()[0]
    assert rec["action"] == "increase_timeout"
    watchdog.apply_remedy(rec["component"], rec["action"], rec["provider"], rec["params"])
    assert store.get_config("maps").timeout == 12.0
    assert not watchdog.apply_remedy("maps", "nonsense")["ok"]


def test_health_report_shape():
    store.record_call("weather:seasonal_model", True, 5)
    rep = watchdog.health_report()
    assert rep["targets"]["weather:seasonal_model"]["calls"] == 1
