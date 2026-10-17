import pytest
from fastapi.testclient import TestClient

from threadroute.agents.runtime import ThreadRouteRuntime
from threadroute.testing import ScriptedLlm, call, say, transfer
from threadroute.ui.app import create_app

PROPOSAL = dict(from_location="ST-HYD-BJH", to_location="ST-BLR-IND", sku="SH-OXF-WHT", size="L", qty=20,
                rationale="stock-out")


@pytest.fixture
def client():
    llm = ScriptedLlm(script=[], seen=[])
    rt = ThreadRouteRuntime(model=llm)
    c = TestClient(create_app(rt))
    c.llm = llm
    yield c


def chat(client, text="move shirts", session="ui-1", **kw):
    client.llm.script = [transfer("allocation_agent"), call("propose_reallocation", **PROPOSAL), say("ACT-1001 queued.")]
    return client.post("/api/chat", data={"message": text, "session_id": session}, **kw)


def test_index_and_status(client):
    assert "ThreadRoute" in client.get("/").text
    s = client.get("/api/status").json()
    assert s["live_apis"] is False and "model" in s
    assert "damaged_carton_SH-1002.png" in client.get("/api/samples").json()
    assert client.get("/samples/damaged_carton_SH-1002.png").status_code == 200


def test_chat_returns_reply_trace_and_pending_actions(client):
    r = chat(client).json()
    assert r["reply"] == "ACT-1001 queued." and r["agent"] == "allocation_agent" and r["message_id"]
    assert r["new_actions"][0]["status"] == "pending" and any(t["name"] == "propose_reallocation" for t in r["trace"])
    assert client.get("/api/actions", params={"status": "pending"}).json()[0]["id"] == "ACT-1001"


def test_approve_edit_and_reject_endpoints(client):
    chat(client)
    ok = client.post("/api/actions/ACT-1001/approve", json={"comment": None, "edits": {"qty": 15}})
    assert ok.status_code == 200 and ok.json()["outcome"]["status"] == "executed"
    assert client.post("/api/actions/ACT-1001/approve", json={}).status_code == 409
    assert client.post("/api/actions/ACT-9999/reject", json={"reason": "x"}).status_code == 409
    chat(client, "again", session="ui-2")
    rej = client.post("/api/actions/ACT-1002/reject", json={"reason": "Never take shirt stock from Hyderabad now."})
    assert rej.status_code == 200 and rej.json()["learned"]["created"]
    assert any("Hyderabad" in g["text"] for g in client.get("/api/guidance").json())


def test_image_upload_validation(client):
    bad = client.post("/api/chat", data={"message": "x"}, files={"image": ("notes.txt", b"hello", "text/plain")})
    assert bad.status_code == 415
    big = client.post("/api/chat", data={"message": "x"},
                      files={"image": ("big.png", b"0" * (8 * 1024 * 1024 + 1), "image/png")})
    assert big.status_code == 413
    assert client.post("/api/chat", data={"message": "  "}).status_code == 400
    client.llm.script = [say("Looks like a carton.")]
    ok = client.post("/api/chat", data={"message": "what is this"}, files={"image": ("a.png", b"\x89PNG", "image/png")})
    assert ok.status_code == 200 and ok.json()["reply"] == "Looks like a carton."


def test_thumbs_feedback_and_teaching(client):
    assert client.post("/api/feedback", json={"session_id": "s", "rating": 1}).json()["ok"]
    down = client.post("/api/feedback", json={"session_id": "s", "rating": -1,
                                              "comment": "Always state the carrier name in delay summaries."}).json()
    assert down["learned"]["created"]
    assert client.post("/api/feedback", json={"session_id": "s", "rating": 5}).status_code == 400
    assert client.post("/api/teach", json={"text": "Prefer rail for folded trousers on long lanes."}).json()["created"]
    gid = client.get("/api/guidance").json()[0]["id"]
    assert client.delete(f"/api/guidance/{gid}").json()["ok"]


def test_health_chaos_and_watchdog_endpoints(client):
    assert client.post("/api/chaos", json={"target": "carrier:primary_api", "mode": "fail"}).status_code == 200
    assert client.post("/api/chaos", json={"target": "carrier:primary_api", "mode": "explode"}).status_code == 400
    for _ in range(3):
        client.get("/mock-carrier/v1/track/SH-1001")  # the mock carrier API is mounted
    from threadroute import integrations
    import asyncio
    asyncio.run(integrations.track_shipment("SH-1001"))
    health = client.get("/api/health").json()
    assert health["chaos_rules"][0]["target"] == "carrier:primary_api"
    assert "carrier:primary_api" in health["targets"] and health["targets"]["carrier:primary_api"]["failures"] >= 1
    assert client.delete("/api/chaos").json()["ok"]
    assert client.post("/api/watchdog/heal").status_code == 200


def test_report_endpoint(client):
    chat(client)
    client.post("/api/actions/ACT-1001/approve", json={})
    rep = client.get("/api/report").json()
    assert "Approval rate" in rep["markdown"] and rep["data"]["proposals"]["approved"] == 1


def test_chat_without_credentials_returns_clear_503():
    c = TestClient(create_app())
    r = c.post("/api/chat", data={"message": "hello"})
    assert r.status_code == 503 and "GOOGLE_API_KEY" in r.json()["detail"]
