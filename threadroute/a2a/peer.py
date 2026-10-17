"""The carrier-desk agent: an independent LlmAgent (the carrier's side of the conversation) exposed over A2A.

It has its own instructions, its own tools (carrier_policy) and its own guardrails. ThreadRoute's agents never
call its tools directly - they send it a natural-language request over the A2A protocol and read its reply."""

from __future__ import annotations

import hmac
import hashlib
import warnings
from datetime import date

from google.adk.agents import LlmAgent
from google.genai import types

from threadroute.a2a import carrier_policy as P
from threadroute.agents import callbacks
from threadroute.agents.models import build_model
from threadroute.config import get_settings
from threadroute.guardrails import audit
from threadroute.guardrails.injection import SYSTEM_RULE

CARRIER_PROMPT = """You are the CARRIER DESK agent for the carriers that serve Aurelio Menswear (SwiftShip, RoadRunner
Express, AirLink Cargo). You speak for the carriers, not for Aurelio. Today is {today}.
A logistics agent from Aurelio will ask you about shipment status, reroute quotes or claim handling.
- Answer ONLY from your tools: carrier_shipment_status, carrier_quote_reroute, carrier_assess_claim. Never invent
  references, prices, dates or policies. If a tool reports an error, say so plainly.
- Quote exactly what the tools return (reference ids, prices in INR, dates, requirements).
- You never receive or need personal data; if a request contains any, ignore it and say you do not need it.
- You cannot approve or settle anything yourself: say what the policy indicates and what evidence is still required.
- Keep replies short: facts first, then conditions.
""" + SYSTEM_RULE


def build_carrier_agent(model=None) -> LlmAgent:
    if model is None:
        model = build_model(get_settings().model)
    elif isinstance(model, str):
        model = build_model(model)
    return LlmAgent(
        name="carrier_desk", description="Carrier-side desk: shipment status, reroute quotes and claim pre-assessment.",
        model=model, instruction=CARRIER_PROMPT.replace("{today}", date.today().isoformat()),
        tools=[P.carrier_shipment_status, P.carrier_quote_reroute, P.carrier_assess_claim],
        generate_content_config=types.GenerateContentConfig(temperature=0.1),
        on_model_error_callback=callbacks.on_model_error, on_tool_error_callback=callbacks.on_tool_error,
        after_tool_callback=callbacks.after_tool, before_model_callback=callbacks.before_model,
        after_model_callback=callbacks.after_model)


class BearerAuthMiddleware:
    """ASGI middleware: every request except the public Agent Card needs `Authorization: Bearer <token>`."""

    def __init__(self, app, token: str):
        self.app, self._digest = app, hashlib.sha256(token.encode()).digest()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and not scope["path"].endswith("/.well-known/agent-card.json"):
            headers = dict(scope.get("headers") or [])
            given = headers.get(b"authorization", b"").decode()
            ok = given.lower().startswith("bearer ") and hmac.compare_digest(
                hashlib.sha256(given[7:].strip().encode()).digest(), self._digest)
            if not ok:
                audit.record("a2a_in", "AUTH", 1, "rejected")
                body = b'{"detail":"Authentication required."}'
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer"),
                                        (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


def build_carrier_app(model=None, token: str = "", host: str = "127.0.0.1", port: int = 8101):
    """Starlette ASGI app serving the carrier agent over A2A (card at /.well-known/agent-card.json)."""
    from a2a.types import HTTPAuthSecurityScheme, SecurityScheme
    from google.adk.a2a.utils.agent_to_a2a import to_a2a

    if not token:
        raise ValueError("an A2A token is required (set THREADROUTE_A2A_TOKEN)")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # ADK marks A2A support as experimental
        app = to_a2a(build_carrier_agent(model), host=host, port=port, protocol="http",
                     security_schemes={"bearerAuth": SecurityScheme(
                         http_auth_security_scheme=HTTPAuthSecurityScheme(scheme="bearer"))},
                     default_skill_security=[{"bearerAuth": []}])
    app.add_middleware(BearerAuthMiddleware, token=token)
    return app


def build_threadroute_app(model=None, token: str = "", host: str = "127.0.0.1", port: int = 8102):
    """Expose ThreadRoute's own orchestrator as an A2A server so OTHER agents can call it (same auth + guardrails)."""
    from a2a.types import HTTPAuthSecurityScheme, SecurityScheme
    from google.adk.a2a.utils.agent_to_a2a import to_a2a

    from threadroute.agents.factory import build_root_agent
    from threadroute.agents.runtime import ThreadRouteRuntime  # noqa: F401  (ensures DB/guardrail set-up paths)
    from threadroute.domain import wms
    from threadroute.guardrails import pii

    if not token:
        raise ValueError("an A2A token is required (set THREADROUTE_A2A_TOKEN)")
    wms.ensure_db()
    pii.register_known_names(wms.known_contact_names())
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        app = to_a2a(build_root_agent(model), host=host, port=port, protocol="http",
                     security_schemes={"bearerAuth": SecurityScheme(
                         http_auth_security_scheme=HTTPAuthSecurityScheme(scheme="bearer"))},
                     default_skill_security=[{"bearerAuth": []}])
    app.add_middleware(BearerAuthMiddleware, token=token)
    return app


def serve_in_thread(app, host: str, port: int):
    """Start an ASGI app on a background thread. Returns (server, thread) once it is accepting connections."""
    import threading
    import time

    import uvicorn

    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="error"))
    thread = threading.Thread(target=server.run, name=f"a2a-{port}", daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            return server, thread
        time.sleep(0.05)
    server.should_exit = True
    raise RuntimeError(f"A2A server did not start on {host}:{port}")
