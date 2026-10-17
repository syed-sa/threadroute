"""Web-layer security: authentication, headers, limits, validation, no information leaks."""

import pytest
from fastapi.testclient import TestClient

from threadroute.agents.runtime import ThreadRouteRuntime
from threadroute.testing import ScriptedLlm, say
from threadroute.ui import security
from threadroute.ui.app import create_app

TOKEN = "s3cret-test-token"


@pytest.fixture
def raw():
    llm = ScriptedLlm(script=[], seen=[])
    c = TestClient(create_app(ThreadRouteRuntime(model=llm), auth_token=TOKEN))
    c.llm = llm
    return c


@pytest.fixture
def authed(raw):
    raw.headers["Authorization"] = f"Bearer {TOKEN}"
    return raw


def test_api_requires_a_valid_token(raw):
    assert raw.get("/api/status").status_code == 401
    assert raw.get("/api/status", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert raw.get("/api/actions", headers={"Authorization": "Basic abc"}).status_code == 401
    assert raw.get("/api/status", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200


def test_every_data_route_is_protected_including_samples(raw):
    for path in ("/api/actions", "/api/health", "/api/report", "/api/guidance", "/api/guardrails", "/api/perf",
                 "/samples/damaged_carton_SH-1002.png"):
        assert raw.get(path).status_code == 401, path
    assert raw.post("/api/chaos", json={"target": "carrier:primary_api", "mode": "fail"}).status_code == 401
    assert raw.delete("/api/chaos").status_code == 401


def test_public_surface_is_only_the_shell_and_health_check(raw):
    assert raw.get("/healthz").json() == {"ok": True}
    assert raw.get("/").status_code == 200 and raw.get("/static/app.js").status_code == 200
    assert raw.get("/mock-carrier/v1/track/SH-1001").status_code in (401, 404)  # not exposed over HTTP


def test_security_headers_and_strict_csp(authed):
    r = authed.get("/")
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp and "frame-ancestors 'none'" in csp
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"
    assert authed.get("/api/status").headers["cache-control"] == "no-store"
    html = r.text
    assert "<script>" not in html and "<style>" not in html   # no inline code: CSP can stay strict


def test_no_api_docs_or_openapi_in_normal_mode(authed):
    assert authed.get("/docs").status_code == 404 and authed.get("/openapi.json").status_code == 404


def test_foreign_host_header_is_rejected(authed):
    assert authed.get("/api/status", headers={"Host": "evil.example.com"}).status_code == 400


def test_chat_input_validation(authed):
    assert authed.post("/api/chat", data={"message": "x" * 4001, "session_id": "a"}).status_code == 413
    assert authed.post("/api/chat", data={"message": "hi", "session_id": "../../etc"}).status_code == 400
    assert authed.post("/api/chat", data={"message": "  "}).status_code == 400


def test_json_bodies_are_validated_and_size_capped(authed):
    assert authed.post("/api/actions/ACT-1001/reject", json={"reason": ""}).status_code == 422
    assert authed.post("/api/actions/not-an-id/reject", json={"reason": "x"}).status_code == 422
    assert authed.post("/api/feedback", json={"session_id": "s", "rating": 5}).status_code == 422
    assert authed.post("/api/chaos", json={"target": "x; DROP", "mode": "fail"}).status_code == 422
    big = {"text": "a" * (security.MAX_JSON_BYTES + 10)}
    assert authed.post("/api/teach", json=big).status_code == 413


def test_rate_limit_returns_429(authed):
    codes = [authed.get("/api/status").status_code for _ in range(security.API_LIMIT_PER_MIN + 5)]
    assert 429 in codes and codes[0] == 200


def test_chat_rate_limit_is_stricter(authed):
    authed.llm.script = [say("ok")] * 40
    codes = [authed.post("/api/chat", data={"message": "hi", "session_id": "r1"}).status_code for _ in range(25)]
    assert codes.count(200) == security.CHAT_LIMIT_PER_MIN and 429 in codes


def test_errors_do_not_leak_internals(monkeypatch):
    llm = ScriptedLlm(script=[], seen=[])
    authed = TestClient(create_app(ThreadRouteRuntime(model=llm), auth_token=TOKEN), raise_server_exceptions=False,
                        headers={"Authorization": f"Bearer {TOKEN}"})
    def boom(*a, **k):
        raise RuntimeError("secret path /home/claude/.env password=hunter2")

    monkeypatch.setattr("threadroute.ui.app.watchdog.health_report", boom)
    r = authed.get("/api/health")
    assert r.status_code == 500 and "hunter2" not in r.text and "/home" not in r.text and "ref" in r.text


def test_token_comparison_helper():
    assert security.token_ok("Bearer abc", "abc") and not security.token_ok("Bearer abd", "abc")
    assert not security.token_ok(None, "abc") and not security.token_ok("abc", "abc")


def test_auth_can_be_disabled_explicitly_for_local_experiments():
    llm = ScriptedLlm(script=[], seen=[])
    c = TestClient(create_app(ThreadRouteRuntime(model=llm), auth_token=None))
    assert c.get("/api/status").status_code == 200
