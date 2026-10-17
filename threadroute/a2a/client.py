"""ThreadRoute's A2A client: lets our agents talk to the carrier-desk agent.

Self-healing transport chain (resilient_call, component 'a2a'):
  a2a_http        the carrier agent running as its own server (THREADROUTE_A2A_URL, started by `web`/`a2a-carrier`)
  a2a_inprocess   the same agent served in this process over the real A2A protocol (ASGI transport)
  local_policy    deterministic carrier policy snapshot (degraded; clearly flagged)
Both directions pass through the guardrails: outbound text is PII/secret-redacted, inbound text is
injection-neutralised and redacted before any of our agents sees it.
"""

from __future__ import annotations

import os
import re
import secrets
import uuid
import warnings

import httpx
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import BaseModel

from threadroute.a2a import carrier_policy as P
from threadroute.guardrails import egress
from threadroute.resilience.resilient import Provider, resilient_call, unavailable_as_error

_SHIPMENT_RE = re.compile(r"\bSH-\d{3,6}\b", re.IGNORECASE)


class A2AReply(BaseModel):
    reply: str
    peer: str = "carrier_desk"


def a2a_token() -> str:
    """Shared secret between ThreadRoute and the carrier desk. Set THREADROUTE_A2A_TOKEN to run them as two
    separate processes; otherwise a random per-run token is generated (fine for a single process)."""
    tok = os.environ.get("THREADROUTE_A2A_TOKEN", "").strip()
    if not tok:
        tok = secrets.token_urlsafe(24)
        os.environ["THREADROUTE_A2A_TOKEN"] = tok
    return tok


class CarrierPeer:
    def __init__(self, model=None):
        self.model = model
        self._inproc: tuple | None = None  # (app, lifespan_cm, httpx client)

    # ------------------------------------------------------------ transports
    async def _run_remote(self, client: httpx.AsyncClient, card_url: str, text: str) -> dict:
        from google.adk.agents.remote_a2a_agent import RemoteA2aAgent

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            remote = RemoteA2aAgent(name="carrier_desk_remote", agent_card=card_url, httpx_client=client,
                                    use_legacy=False, timeout=55.0)
            sessions = InMemorySessionService()
            runner = Runner(agent=remote, app_name="tr-a2a", session_service=sessions)
            sid = uuid.uuid4().hex
            await sessions.create_session(app_name="tr-a2a", user_id="threadroute", session_id=sid)
            texts: list[str] = []
            problem = ""
            async for ev in runner.run_async(user_id="threadroute", session_id=sid,
                                             new_message=types.Content(role="user", parts=[types.Part(text=text)])):
                if getattr(ev, "error_message", None):
                    problem = ev.error_message
                if ev.content and ev.content.parts:
                    t = "".join(p.text for p in ev.content.parts if getattr(p, "text", None)
                                and not getattr(p, "thought", False))
                    if t.strip():
                        texts.append(t.strip())
        if not texts:
            raise RuntimeError(problem or "carrier agent returned no reply")
        return {"reply": texts[-1], "peer": "carrier_desk"}

    async def _http(self, text: str) -> dict:
        base = os.environ.get("THREADROUTE_A2A_URL", "").rstrip("/")
        if not base:
            raise ConnectionError("no external carrier agent configured (THREADROUTE_A2A_URL)")
        async with httpx.AsyncClient(headers={"Authorization": f"Bearer {a2a_token()}"}, timeout=60) as client:
            return await self._run_remote(client, f"{base}/.well-known/agent-card.json", text)

    async def _ensure_inproc(self):
        if self._inproc is None:
            from threadroute.a2a.peer import build_carrier_app

            app = build_carrier_app(self.model, token=a2a_token())
            cm = app.router.lifespan_context(app)
            await cm.__aenter__()
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8101",
                                       headers={"Authorization": f"Bearer {a2a_token()}"}, timeout=60)
            self._inproc = (app, cm, client)
        return self._inproc

    async def _inprocess(self, text: str) -> dict:
        _app, _cm, client = await self._ensure_inproc()
        return await self._run_remote(client, "http://127.0.0.1:8101/.well-known/agent-card.json", text)

    async def _local(self, text: str) -> dict:
        m = _SHIPMENT_RE.search(text or "")
        if not m:
            return {"reply": "Carrier desk is unreachable and no shipment id was given, so no policy snapshot is "
                             "possible. Retry shortly."}
        sid = m.group(0).upper()
        status = P.carrier_shipment_status(sid)
        quotes = [P.carrier_quote_reroute(sid, mode) for mode in ("road", "rail", "air")]
        quotes = [q for q in quotes if q.get("ok")]
        lines = [f"[local policy snapshot - carrier desk unreachable] {sid}: {status}"]
        lines += [f"indicative {q['mode']} quote via {q['carrier']}: INR {q['price_inr']}, arrival {q['est_arrival']}"
                  for q in quotes]
        return {"reply": "\n".join(lines)}

    # ------------------------------------------------------------ public API
    @unavailable_as_error
    async def _ask(self, text: str) -> dict:
        providers = []
        if os.environ.get("THREADROUTE_A2A_URL"):
            providers.append(Provider("a2a_http", self._http))
        providers += [Provider("a2a_inprocess", self._inprocess), Provider("local_policy", self._local, degraded=True)]
        return await resilient_call("a2a", providers, text, schema=A2AReply)

    async def ask(self, question: str, shipment_id: str = "") -> dict:
        q = question.strip()
        if shipment_id and shipment_id.upper() not in q.upper():
            q = f"{q} (shipment {shipment_id.upper()})"
        out = egress.protect("a2a_out", q)               # nothing personal leaves in a message to another agent
        result = await self._ask(out)
        if result.get("ok") is False:
            return result
        reply = egress.protect("a2a_in", result["reply"], sanitize_untrusted=True)  # peer text is untrusted data
        out_res = {"ok": True, "peer": result.get("peer", "carrier_desk"), "transport": result.get("source"),
                   "reply": reply, "degraded": result.get("degraded", False)}
        if result.get("healing_note"):
            out_res["healing_note"] = result["healing_note"]
        return out_res

    async def aclose(self) -> None:
        if self._inproc:
            _app, cm, client = self._inproc
            self._inproc = None
            try:
                await client.aclose()
                await cm.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass


_peer: CarrierPeer | None = None


def set_peer(peer: CarrierPeer | None) -> None:
    global _peer
    _peer = peer


def get_peer() -> CarrierPeer:
    global _peer
    if _peer is None:
        _peer = CarrierPeer()
    return _peer
