"""Exercises the real ADK runner, agent transfers, MCP toolsets, callbacks and approval gate
with a scripted model standing in for Gemini."""

from typing import AsyncGenerator

import pytest
from google.adk.models.base_llm import BaseLlm, LlmCapabilities
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset

from threadroute import integrations
from threadroute.agents import callbacks
from threadroute.agents.factory import build_root_agent
from threadroute.agents.runtime import MissingCredentials, ThreadRouteRuntime
from threadroute.approvals import service
from threadroute.feedback import store as fb
from threadroute.mcp_server.server import WRITE_TOOLS
from threadroute.resilience import chaos, store
from threadroute.testing import ScriptedLlm, call, say, transfer


def tools_called(result):
    return [t["name"] for t in result.trace if t["type"] in ("tool_call", "transfer")]


def instruction_of(req) -> str:
    si = req.config.system_instruction
    return si if isinstance(si, str) else " ".join(p.text or "" for p in si.parts)


ALLOC_PROPOSAL = dict(from_location="ST-HYD-BJH", to_location="ST-BLR-IND", sku="SH-OXF-WHT", size="L", qty=30,
                      rationale="Bengaluru has 0 stock")


async def test_orchestrator_routes_to_specialist_and_queues_proposal(runtime, scripted):
    scripted.script = [transfer("allocation_agent"),
                       call("find_donor_stores", sku="SH-OXF-WHT", size="L", to_location_id="ST-BLR-IND", needed_qty=30),
                       call("propose_reallocation", **ALLOC_PROPOSAL),
                       say("Proposed ACT-1001 - awaiting your approval.")]
    r = await runtime.chat("s1", "Bengaluru is out of white Oxford shirts in L")
    assert r.error is None and r.agent == "allocation_agent" and "ACT-1001" in r.reply
    assert tools_called(r) == ["transfer_to_agent", "find_donor_stores", "propose_reallocation"]
    assert [(a["kind"], a["status"]) for a in r.new_actions] == [("reallocate_stock", "pending")]
    # the MCP tool really ran and returned live data
    donor_result = next(t for t in r.trace if t["type"] == "tool_result" and t["name"] == "find_donor_stores")
    assert "ST-HYD-BJH" in donor_result["result"]


async def test_agents_cannot_reach_write_tools():
    root = build_root_agent(model=ScriptedLlm(script=[], seen=[]))
    for agent in root.sub_agents:
        for tool in agent.tools:
            if isinstance(tool, McpToolset):
                assert not set(tool.tool_filter) & set(WRITE_TOOLS), agent.name
    names = {t.__name__ for a in root.sub_agents for t in a.tools if callable(t) and not isinstance(t, McpToolset)}
    assert not {"approve", "reject", "execute"} & names  # no tool lets the LLM approve its own proposals


async def test_invalid_proposal_is_fed_back_and_repaired(runtime, scripted):
    seen_status = []

    def repair(req):
        seen_status.append(req.contents[-1].parts[0].function_response.response)
        return call("propose_reallocation", **{**ALLOC_PROPOSAL, "qty": 20})

    scripted.script = [transfer("allocation_agent"),
                       call("propose_reallocation", **{**ALLOC_PROPOSAL, "qty": 500}),  # exceeds donor surplus
                       repair, say("Fixed it: ACT-1001 awaits approval.")]
    r = await runtime.chat("s1", "move shirts")
    assert seen_status[0]["status"] == "invalid" and "safety cover" in seen_status[0]["errors"][0]
    assert len(r.new_actions) == 1 and r.new_actions[0]["payload"]["qty"] == 20


async def test_schema_errors_from_the_model_are_reported_back(runtime, scripted):
    seen = []

    def check(req):
        seen.append(req.contents[-1].parts[0].function_response.response)
        return say("I could not queue that.")

    scripted.script = [transfer("exceptions_claims_agent"),
                       call("propose_file_claim", shipment_id="SH-1002", claim_type="theft", damage_type="crushed_carton",
                            severity="moderate", affected_units=5, evidence_summary="short"),
                       check]
    r = await runtime.chat("s1", "claim")
    assert seen[0]["status"] == "invalid" and any("claim_type" in e for e in seen[0]["errors"])
    assert r.new_actions == []


async def test_identical_proposals_are_deduplicated(runtime, scripted):
    scripted.script = [transfer("allocation_agent"), call("propose_reallocation", **ALLOC_PROPOSAL),
                       call("propose_reallocation", **ALLOC_PROPOSAL), say("done")]
    await runtime.chat("s1", "move shirts")
    assert len(service.list_actions(session_id="s1")) == 1


async def test_learned_guidance_is_injected_into_the_specialist_prompt(runtime, scripted):
    fb.learn_guidance("Never take Oxford shirt stock from Hyderabad - use Chennai first.", "allocation")
    scripted.script = [transfer("allocation_agent"), say("Understood.")]
    await runtime.chat("s1", "Bengaluru needs Oxford shirts in L")
    allocation_request = scripted.seen[1]
    assert "LEARNED OPERATING GUIDANCE" in instruction_of(allocation_request)
    assert "use Chennai first" in instruction_of(allocation_request)
    assert "LEARNED OPERATING GUIDANCE" not in instruction_of(scripted.seen[0])  # orchestrator: rule is not relevant


async def test_image_reaches_the_model(runtime, scripted):
    scripted.script = [say("I see an image.")]
    await runtime.chat("s1", "what is this?", images=[(b"\x89PNG-not-really", "image/png")])
    parts = [p for c in scripted.seen[0].contents for p in (c.parts or [])]
    assert any(getattr(p, "inline_data", None) and p.inline_data.mime_type == "image/png" for p in parts)


async def test_image_findings_flow_from_intake_to_claims_agent(runtime, scripted):
    scripted.script = [
        transfer("intake_agent"),
        call("record_image_findings", image_type="damaged_carton", summary="Two cartons with crushed corners.",
             shipment_id="sh-1002", damage_type="crushed_carton", severity="moderate", affected_units=12),
        transfer("exceptions_claims_agent"), call("get_image_findings"),
        call("propose_file_claim", shipment_id="SH-1002", claim_type="damage", damage_type="crushed_carton",
             severity="moderate", affected_units=12, evidence_summary="Photo shows crushed corners on two cartons."),
        say("Queued claim ACT-1001."),
    ]
    r = await runtime.chat("s1", "look at this", images=[(b"img", "image/png")])
    findings = next(t for t in r.trace if t["type"] == "tool_result" and t["name"] == "get_image_findings")
    assert "SH-1002" in findings["result"] and "crushed_carton" in findings["result"]
    assert {t["agent"] for t in r.trace} >= {"intake_agent", "exceptions_claims_agent"}
    assert r.new_actions[0]["kind"] == "file_claim"


async def test_invalid_image_findings_are_rejected_with_guidance(runtime, scripted):
    seen = []

    def check(req):
        seen.append(req.contents[-1].parts[0].function_response.response)
        return say("ok")

    scripted.script = [transfer("intake_agent"),
                       call("record_image_findings", image_type="selfie", summary="Not a logistics image at all."), check]
    await runtime.chat("s1", "x", images=[(b"img", "image/png")])
    assert seen[0]["status"] == "invalid"


async def test_tool_exception_becomes_structured_error_and_chat_continues(runtime, scripted, monkeypatch):
    async def boom(_):
        raise ConnectionError("carrier gateway exploded")

    monkeypatch.setattr(integrations, "track_shipment", boom)
    scripted.script = [transfer("tracking_agent"), call("get_carrier_tracking", shipment_id="SH-1001"),
                       say("The carrier feed is unavailable right now.")]
    r = await runtime.chat("s1", "where is SH-1001?")
    result = next(t for t in r.trace if t["type"] == "tool_result" and t["name"] == "get_carrier_tracking")
    assert "'status': 'error'" in result["result"] and "carrier gateway exploded" in result["result"]
    assert r.error is None and "unavailable" in r.reply
    assert any(e["kind"] == "tool_error" for e in store.recent_events())


async def test_degraded_data_is_surfaced_to_the_session(runtime, scripted):
    chaos.set_rule("carrier:primary_api", "fail")
    chaos.set_rule("carrier:secondary_api", "fail")
    scripted.script = [transfer("tracking_agent"), call("get_carrier_tracking", shipment_id="SH-1001"), say("Delayed.")]
    r = await runtime.chat("s1", "where is SH-1001?")
    assert r.degraded and "get_carrier_tracking" in r.degraded[0]


class FailingLlm(BaseLlm):
    model: str = "primary-x"

    @property
    def capabilities(self) -> LlmCapabilities:
        return LlmCapabilities(output_schema_and_tools=False)

    async def generate_content_async(self, llm_request, stream=False) -> AsyncGenerator:
        raise RuntimeError("503 UNAVAILABLE: model overloaded")
        yield  # pragma: no cover


async def test_model_outage_falls_back_to_next_model(monkeypatch):
    monkeypatch.setenv("THREADROUTE_FALLBACK_MODELS", "backup-model")
    backup = ScriptedLlm(model="backup-model", script=[say("Answer from the backup model.")], seen=[])
    monkeypatch.setattr(callbacks, "build_model", lambda name: backup)
    rt = ThreadRouteRuntime(model=FailingLlm())
    try:
        r = await rt.chat("s1", "hello")
    finally:
        await rt.close()
    assert r.reply == "Answer from the backup model." and r.error is None
    kinds = [(e["kind"], e["provider"]) for e in store.recent_events()]
    assert ("model_fallback", "backup-model") in kinds
    assert backup.seen[0].model == "backup-model"


async def test_total_model_outage_degrades_gracefully(monkeypatch):
    monkeypatch.setenv("THREADROUTE_FALLBACK_MODELS", "backup-model")
    monkeypatch.setattr(callbacks, "build_model", lambda name: FailingLlm(model=name))
    rt = ThreadRouteRuntime(model=FailingLlm())
    try:
        r = await rt.chat("s1", "hello")
    finally:
        await rt.close()
    assert "could not reach the AI model" in r.reply and r.error is None
    assert any(e["kind"] == "exhausted" and e["component"] == "llm" for e in store.recent_events())


async def test_watchdog_agent_can_inspect_and_heal(runtime, scripted):
    for _ in range(4):
        store.record_call("carrier:primary_api", False, 10, "ConnectionError: boom")
    scripted.script = [transfer("watchdog_agent"), call("get_health_report"), call("run_auto_heal"),
                       say("Demoted the failing carrier API.")]
    r = await runtime.chat("s1", "is the carrier feed down?")
    healed = next(t for t in r.trace if t["type"] == "tool_result" and t["name"] == "run_auto_heal")
    assert "demote_provider" in healed["result"]
    assert "primary_api" in store.get_config("carrier").active_demotions()


async def test_reroute_options_tool_compares_alternatives():
    from threadroute.agents import tools as T

    res = await T.get_reroute_options("SH-1001")
    alts = {(o["carrier"], o["mode"]): o for o in res["alternatives"]}
    assert ("AirLink Cargo", "air") in alts and ("RoadRunner Express", "road") in alts
    assert res["weather_at_current_position"]["risk_level"] == "severe"
    assert alts[("AirLink Cargo", "air")]["exposed_to_weather_risk"] is False
    assert alts[("RoadRunner Express", "road")]["exposed_to_weather_risk"] is True
    assert alts[("AirLink Cargo", "air")]["est_cost_inr"] > alts[("RoadRunner Express", "road")]["est_cost_inr"]
    assert res["alternatives"] == sorted(res["alternatives"], key=lambda o: o["est_arrival_date"])
    assert res["current_plan"]["tracking_source"] == "primary_api"
    delivered = await T.get_reroute_options("SH-1002")
    assert delivered["ok"] is False and delivered["error"]["code"] == "NOT_REROUTABLE"


async def test_reroute_options_degrade_gracefully_when_feeds_fail():
    from threadroute.agents import tools as T

    chaos.set_rule("weather:seasonal_model", "fail")
    chaos.set_rule("carrier:primary_api", "fail")
    res = await T.get_reroute_options("SH-1001")
    assert res["weather_at_current_position"]["risk_level"] == "unknown"
    assert res["current_plan"]["tracking_source"] == "secondary_api"


async def test_total_feed_outage_returns_structured_error_not_exception():
    chaos.set_rule("weather:*", "fail")
    chaos.set_rule("carrier:*", "fail")
    weather = await integrations.get_weather_risk("Nagpur Hub")
    assert weather["ok"] is False and weather["error"]["code"] == "SERVICE_UNAVAILABLE"
    tracking = await integrations.track_shipment("SH-1001")
    assert tracking["error"]["component"] == "carrier"
    from threadroute.agents import tools as T

    res = await T.get_reroute_options("SH-1001")  # still returns a usable comparison
    assert res["weather_at_current_position"]["risk_level"] == "unknown" and res["alternatives"]


async def test_conversation_continues_with_the_same_specialist(runtime, scripted):
    scripted.script = [transfer("tracking_agent"), say("SH-1001 is in Nagpur."), say("It is 4 days late.")]
    await runtime.chat("s1", "where is SH-1001?")
    r2 = await runtime.chat("s1", "and how late is it?")
    assert r2.agent == "tracking_agent" and "4 days" in r2.reply


def test_missing_credentials_are_reported_clearly():
    with pytest.raises(MissingCredentials) as exc:
        ThreadRouteRuntime()
    assert "GOOGLE_API_KEY" in str(exc.value)
