import pytest

from threadroute.approvals import service
from threadroute.domain import wms
from threadroute.feedback import store as fb
from threadroute.resilience import chaos, store

REALLOC = {"from_location": "ST-HYD-BJH", "to_location": "ST-BLR-IND", "sku": "SH-OXF-WHT", "size": "L", "qty": 20,
           "reason": "stock-out"}
CLAIM = {"shipment_id": "SH-1002", "claim_type": "damage", "damage_type": "crushed_carton", "severity": "moderate",
         "affected_units": 12, "evidence_summary": "Photo shows crushed corners on two hanging-garment cartons."}


def make(kind="reallocate_stock", payload=None, guidance=None):
    return service.create("s1", "allocation_agent", kind, payload or REALLOC, "summary", "why", guidance or [])


async def test_pending_action_changes_nothing_until_approved():
    a = make()
    assert a["status"] == "pending" and a["id"].startswith("ACT-")
    assert wms.get_inventory(location_id="ST-BLR-IND", sku="SH-OXF-WHT", size="L")[0]["qty"] == 0


async def test_approve_executes_via_mcp_and_logs():
    a = make()
    res = await service.approve(a["id"], comment="ok")
    assert res["ok"] and res["outcome"]["status"] == "executed"
    steps = {s["step"]: s for s in res["outcome"]["steps"]}
    assert steps["reallocate_stock"]["detail"]["source"] == "mcp_stdio"
    assert "log_to_sheet" in steps
    assert wms.get_inventory(location_id="ST-BLR-IND", sku="SH-OXF-WHT", size="L")[0]["qty"] == 20
    assert service.get(a["id"])["status"] == "executed"


async def test_cannot_decide_twice():
    a = make()
    await service.approve(a["id"])
    again = await service.approve(a["id"])
    assert again["ok"] is False and "already" in again["error"]
    assert (await service.reject(a["id"], "too late"))["ok"] is False


async def test_reject_learns_guidance_and_executes_nothing():
    a = make(guidance=[])
    res = await service.reject(a["id"], "Never take shirt stock from Hyderabad during the festive promotion.")
    assert res["learned"]["created"]
    assert wms.get_inventory(location_id="ST-BLR-IND", sku="SH-OXF-WHT", size="L")[0]["qty"] == 0
    assert service.get(a["id"])["status"] == "rejected"


async def test_guidance_outcomes_flow_back_to_rules():
    gid = fb.learn_guidance("Prefer Chennai as donor for Bengaluru shirts.", "allocation")["id"]
    ok = make(guidance=[gid])
    await service.approve(ok["id"])
    assert fb.list_guidance()[0]["helpful"] == 1
    bad = make(payload={**REALLOC, "qty": 5}, guidance=[gid])
    await service.reject(bad["id"], "Still wrong - the donor has a promotion running this week.")
    row = [g for g in fb.list_guidance(active_only=False) if g["id"] == gid][0]
    assert row["unhelpful"] == 1 and row["uses"] == 2


async def test_edits_are_validated_and_applied():
    a = make()
    bad = await service.approve(a["id"], edits={"qty": 0})
    assert bad["ok"] is False and "qty" in bad["error"]
    assert service.get(a["id"])["status"] == "pending"
    good = await service.approve(a["id"], comment="Move fewer units, keep Hyderabad cover.", edits={"qty": 10})
    assert good["ok"] and wms.get_inventory(location_id="ST-BLR-IND", sku="SH-OXF-WHT", size="L")[0]["qty"] == 10
    assert good["learned"]  # an edit with a comment teaches a rule
    assert fb.list_guidance()


async def test_execution_failure_is_reported_not_hidden():
    a = make(payload={**REALLOC, "qty": 40})
    wms.reallocate_stock("ST-HYD-BJH", "ST-CHN-TNG", "SH-OXF-WHT", "L", 30)  # stock changed since the proposal
    res = await service.approve(a["id"])
    assert res["outcome"]["status"] == "failed"
    assert res["outcome"]["result"]["error"]["code"] == "INSUFFICIENT_SURPLUS"
    assert service.get(a["id"])["status"] == "failed"


async def test_mcp_outage_falls_back_to_direct_domain_call():
    chaos.set_rule("mcp:mcp_stdio", "fail")
    a = make()
    res = await service.approve(a["id"])
    step = res["outcome"]["steps"][0]
    assert res["outcome"]["status"] == "executed" and step["detail"]["source"] == "direct_domain"
    assert wms.get_inventory(location_id="ST-BLR-IND", sku="SH-OXF-WHT", size="L")[0]["qty"] == 20
    assert any(e["component"] == "mcp" and e["kind"] == "recovered" for e in store.recent_events())


async def test_claim_flow_files_claim_emails_carrier_and_logs():
    a = make("file_claim", CLAIM)
    res = await service.approve(a["id"])
    assert [s["step"] for s in res["outcome"]["steps"]] == ["create_claim", "email_carrier", "log_to_sheet"]
    claim = wms.list_claims("SH-1002")[0]
    assert claim["claim_value"] == 34800.0
    import json
    from threadroute.config import get_settings
    mail = json.loads(next(get_settings().outbox_dir.glob("*.json")).read_text(encoding="utf-8"))
    assert "CL-2001" in mail["subject"] and "RoadRunner Express" in mail["body"] and "34,800.00" in mail["body"]


async def test_reroute_return_and_email_actions():
    r = make("reroute_shipment", {"shipment_id": "SH-1001", "new_mode": "air", "new_carrier": "AirLink Cargo",
                                  "reason": "road closed", "new_eta": None})
    assert (await service.approve(r["id"]))["outcome"]["status"] == "executed"
    assert wms.get_shipment("SH-1001")["carrier"] == "AirLink Cargo"
    d = make("set_return_disposition", {"return_id": "RT-9001", "grade": "C", "disposition": "liquidate", "notes": "stain"})
    assert (await service.approve(d["id"]))["outcome"]["status"] == "executed"
    e = make("send_email", {"to": "store.st-del-cp@aurelio-menswear.example.com", "subject": "SH-1001 delay",
                            "body": "Shipment SH-1001 is delayed by four days due to flooding."})
    assert (await service.approve(e["id"]))["outcome"]["status"] == "executed"


async def test_list_filters():
    a = make()
    make("send_email", {"to": "a@b.com", "subject": "Hello", "body": "A long enough email body."})
    await service.reject(a["id"], "no")
    assert len(service.list_actions(status="pending")) == 1
    assert len(service.list_actions(session_id="s1")) == 2
    assert service.list_actions(session_id="other") == []
