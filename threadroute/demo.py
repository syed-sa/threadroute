"""Offline end-to-end demo: python -m threadroute demo   (no API key, no internet).

The real stack runs: ADK runner and agent transfers, MCP server subprocesses, approval gate, feedback
loop, resilient integrations, watchdog. Only the LLM is replaced by a scripted model so the run is
deterministic. With a Gemini key the same flows are driven by the real model (see README)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from threadroute.config import REPO_ROOT


def _title(n: int, text: str) -> None:
    print(f"\n{'=' * 72}\n STEP {n}: {text}\n{'=' * 72}")


def _instruction_text(request) -> str:
    si = request.config.system_instruction
    if isinstance(si, str):
        return si
    parts = getattr(si, "parts", None) or []
    return " ".join(getattr(p, "text", "") or "" for p in parts)


async def run_demo() -> int:
    home = tempfile.mkdtemp(prefix="threadroute-demo-")
    os.environ["THREADROUTE_HOME"] = home
    os.environ["THREADROUTE_LIVE_APIS"] = "false"

    from threadroute import integrations
    from threadroute.agents.runtime import ThreadRouteRuntime
    from threadroute.approvals import service as approvals
    from threadroute.cli import format_action
    from threadroute.data.seed import seed_database
    from threadroute.domain import wms
    from threadroute.config import get_settings
    from threadroute.feedback import store as fb
    from threadroute.feedback.report import build_report_markdown
    from threadroute.resilience import chaos, store as rstore
    from threadroute.testing import ScriptedLlm, call, say, transfer

    _title(1, "Seed the mock warehouse/order system (fictional brand: Aurelio Menswear)")
    print("  ", seed_database(get_settings().wms_db))
    print(f"   data folder: {home}")
    rstore.set_config("carrier", base_delay=0.05)

    # ------------------------------------------------------------------ step 2
    _title(2, "Multi-agent conversation: orchestrator -> allocation agent -> MCP tools -> proposal")
    print("   (LLM = scripted stand-in; agents, transfers, MCP subprocesses and approval gate are real)")
    llm = ScriptedLlm(script=[
        transfer("allocation_agent"),
        call("get_inventory", location_id="ST-BLR-IND", sku="SH-OXF-WHT", size="L"),
        call("find_donor_stores", sku="SH-OXF-WHT", size="L", to_location_id="ST-BLR-IND", needed_qty=40),
        call("propose_reallocation", from_location="ST-HYD-BJH", to_location="ST-BLR-IND", sku="SH-OXF-WHT",
             size="L", qty=40, rationale="Bengaluru has 0 stock and sells ~14/week; Hyderabad keeps 9 units of cover."),
        say("Bengaluru Indiranagar is out of White Oxford shirts in L (14/week demand). Hyderabad can release 43 units "
            "and is 501 km away. I proposed moving 40 units - ACT-1001 is waiting for your approval."),
    ], seen=[])
    rt = ThreadRouteRuntime(model=llm)
    r1 = await rt.chat("demo-1", "Bengaluru is out of white Oxford shirts in L - fix it")
    for t in r1.trace:
        if t["type"] in ("transfer", "tool_call"):
            print(f"   [{t['agent']}] {t['name']} {t['args'][:80]}")
    print(f"\n   {r1.agent}> {r1.reply}")
    for a in r1.new_actions:
        print(format_action(a))

    # ------------------------------------------------------------------ step 3
    _title(3, "Feedback loop: a manager rejects with a reason -> the agent learns a rule")
    reason = ("Never take Oxford shirt stock from Hyderabad - Banjara Hills runs a festive promotion. "
              "Use Chennai first.")
    res = await approvals.reject(r1.new_actions[0]["id"], reason)
    print(f"   REJECT ACT-1001: \"{reason}\"")
    print(f"   learned rule -> {res['learned']}")
    llm2 = ScriptedLlm(script=[
        transfer("allocation_agent"),
        call("find_donor_stores", sku="SH-OXF-WHT", size="L", to_location_id="ST-BLR-IND", needed_qty=18),
        call("propose_reallocation", from_location="ST-CHN-TNG", to_location="ST-BLR-IND", sku="SH-OXF-WHT",
             size="L", qty=18, rationale="Chennai is the nearest donor (281 km) and Hyderabad is excluded by the rule."),
        say("Following the rule your team taught me (not Hyderabad), I proposed 18 units from Chennai T Nagar, "
            "the nearest donor - ACT-1002 awaits approval. It covers about 1.3 weeks of Bengaluru demand."),
    ], seen=[])
    llm.script, llm.seen = llm2.script, []
    r2 = await rt.chat("demo-2", "Bengaluru still has no Oxford shirts in size L, what now?")
    injected = [_instruction_text(req) for req in llm.seen if "LEARNED OPERATING GUIDANCE" in _instruction_text(req)]
    print(f"   rule injected into the agent's instructions on {len(injected)} model call(s):")
    if injected:
        block = injected[0][injected[0].index("LEARNED OPERATING GUIDANCE"):].splitlines()[:2]
        print("     " + "\n     ".join(l[:150] for l in block))
    for a in r2.new_actions:
        print(format_action(a))

    # ------------------------------------------------------------------ step 4
    _title(4, "Human approval executes through the MCP write path, logs to the action log")
    before = wms.get_inventory(location_id="ST-BLR-IND", sku="SH-OXF-WHT", size="L")[0]["qty"]
    out = await approvals.approve(r2.new_actions[0]["id"], comment="Good - nearest donor first is right.")
    print(f"   ACT status: {out['outcome']['status']}  steps: {[(s['step'], s['ok']) for s in out['outcome']['steps']]}")
    after = wms.get_inventory(location_id="ST-BLR-IND", sku="SH-OXF-WHT", size="L")[0]["qty"]
    print(f"   Bengaluru L stock: {before} -> {after}")

    # ------------------------------------------------------------------ step 5
    _title(5, "Multimodal: a damage photo reaches the model, findings flow between agents, claim is filed")
    sample = REPO_ROOT / "samples" / "damaged_carton_SH-1002.png"
    img = sample.read_bytes() if sample.exists() else b"\x89PNG\r\n"
    llm.script = [
        transfer("intake_agent"),
        call("record_image_findings", image_type="damaged_carton", summary="Two cartons with crushed corners and torn tape.",
             shipment_id="SH-1002", damage_type="crushed_carton", severity="moderate", affected_units=12,
             visible_text="SH-1002 HANGING GARMENTS"),
        transfer("exceptions_claims_agent"),
        call("get_image_findings"),
        call("propose_file_claim", shipment_id="SH-1002", claim_type="damage", damage_type="crushed_carton",
             severity="moderate", affected_units=12,
             evidence_summary="Photo shows two hanging-garment cartons with crushed corners and torn tape on delivery."),
        say("The photo shows crushed corners on SH-1002 (about 12 blazers affected). I queued a damage claim: ACT-1003."),
    ]
    llm.seen = []
    r3 = await rt.chat("demo-3", "Cartons arrived at Delhi CP like this - what do we do?", images=[(img, "image/png")])
    sent = any(getattr(p, "inline_data", None) for req in llm.seen for c in req.contents for p in (c.parts or []))
    print(f"   image part delivered to the model: {sent}  ({len(img)} bytes)")
    print(f"   agents involved: {sorted({t['agent'] for t in r3.trace})}")
    claim = r3.new_actions[0]
    out = await approvals.approve(claim["id"])
    print(f"   approved {claim['id']}: {out['outcome']['status']}  steps: {[s['step'] for s in out['outcome']['steps']]}")
    mails = sorted(Path(home, "outbox").glob("*.json"))
    print(f"   carrier email written to outbox ({len(mails)} file) - subject: "
          f"{__import__('json').loads(mails[-1].read_text())['subject']}")
    await rt.close()

    # ------------------------------------------------------------------ step 6
    _title(6, "Self-healing: kill the carrier API, then break the fallback, then recover")
    r = await integrations.track_shipment("SH-1001")
    print(f"   normal           -> source={r['source']:<14} degraded={r['degraded']}  status={r['status']}")
    chaos.set_rule("carrier:primary_api", "fail")
    r = await integrations.track_shipment("SH-1001")
    print(f"   primary API down -> source={r['source']:<14} degraded={r['degraded']}  note: {r.get('healing_note')}")
    chaos.set_rule("carrier:secondary_api", "malformed")
    for _ in range(2):
        r = await integrations.track_shipment("SH-1001")
    print(f"   + bad payloads   -> source={r['source']:<14} degraded={r['degraded']}  note: {r.get('healing_note')}")
    demoted = rstore.get_config("carrier").active_demotions()
    print(f"   watchdog auto-demoted failing providers: {demoted}")
    chaos.clear()
    r = await integrations.track_shipment("SH-1001")
    print(f"   chaos cleared    -> source={r['source']:<14} degraded={r['degraded']}  (fully recovered)")
    print("   healing events recorded:", {k: v for k, v in sorted(
        __import__('collections').Counter(e['kind'] for e in rstore.recent_events(60, 500)).items())})

    _title(7, "Feedback + healing report")
    print(build_report_markdown())
    print("Demo complete. Next: add your Gemini key to .env and run `python -m threadroute chat` or `web`.")
    return 0
