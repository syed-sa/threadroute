"""Chat runtime: wraps the ADK Runner and returns a simple, UI-friendly ChatResult."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from threadroute.agents.factory import build_root_agent
from threadroute.approvals import service as approvals
from threadroute import evidence
from threadroute.evaluation import grounding
from threadroute.config import get_settings
from threadroute.domain import wms
from threadroute.feedback import store as fb
from threadroute.guardrails import audit, egress, pii
from threadroute.guardrails import images as images_mod
from threadroute.perf.metrics import metrics
from threadroute.resilience import store as rstore

APP_NAME = "threadroute"


def verify_enabled() -> bool:
    import os

    return os.environ.get("THREADROUTE_VERIFY", "1").strip().lower() not in {"0", "false", "off", "no"}


class MissingCredentials(RuntimeError):
    pass


@dataclass
class ChatResult:
    reply: str
    agent: str = "orchestrator"
    trace: list[dict] = field(default_factory=list)
    new_actions: list[dict] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)
    error: str | None = None
    notices: list[str] = field(default_factory=list)  # guardrail messages shown to the user
    verification: dict | None = None  # grounding / truthfulness check of the answer

    def to_dict(self) -> dict:
        return asdict(self)


def _short(obj, limit: int = 300) -> str:
    text = str(obj)
    return text if len(text) <= limit else text[:limit] + "..."


class ThreadRouteRuntime:
    def __init__(self, model=None):
        if model is None and not get_settings().has_gemini_credentials:
            raise MissingCredentials(
                "No Gemini credentials found. Set GOOGLE_API_KEY in .env (get a free key at "
                "https://aistudio.google.com/apikey) or configure Vertex AI. See README, step 4.")
        wms.ensure_db()
        pii.register_known_names(wms.known_contact_names())
        fb.ensure()
        from threadroute.a2a.client import CarrierPeer, set_peer

        self.peer = CarrierPeer(model)  # the independent carrier-desk agent we talk to over A2A
        set_peer(self.peer)
        self.root = build_root_agent(model)
        self.sessions = InMemorySessionService()
        self.runner = Runner(agent=self.root, app_name=APP_NAME, session_service=self.sessions)

    async def _ensure_session(self, session_id: str, user_id: str):
        session = await self.sessions.get_session(app_name=APP_NAME, user_id=user_id, session_id=session_id)
        if session is None:
            await self.sessions.create_session(app_name=APP_NAME, user_id=user_id, session_id=session_id)

    async def _run_turn(self, message, session_id: str, user_id: str, trace: list, replies: list, on_event=None):
        """Run one model turn through the ADK runner, collecting trace + final replies. Returns an error name or None."""
        try:
            async for event in self.runner.run_async(user_id=user_id, session_id=session_id, new_message=message):
                for call in event.get_function_calls():
                    kind = "transfer" if call.name == "transfer_to_agent" else "tool_call"
                    item = {"type": kind, "agent": event.author, "name": call.name, "args": _short(dict(call.args or {}))}
                    trace.append(item)
                    if on_event:
                        on_event(egress.protect("user_reply", item))
                for resp in event.get_function_responses():
                    trace.append({"type": "tool_result", "agent": event.author, "name": resp.name,
                                  "result": _short(resp.response)})
                if event.content and event.content.parts and not event.partial and event.is_final_response():
                    text_out = "".join(p.text for p in event.content.parts if p.text and not getattr(p, "thought", False))
                    if text_out.strip():
                        replies.append((event.author, text_out.strip()))
        except Exception as exc:  # noqa: BLE001 - a chat turn must never crash the UI/CLI
            rstore.log_event("agent_runtime", None, "failure", f"{type(exc).__name__}: {str(exc)[:200]}")
            return type(exc).__name__
        return None

    async def chat(self, session_id: str, text: str, images: list[tuple[bytes, str]] | None = None,
                   user_id: str = "ops", on_event=None) -> ChatResult:
        imgs = images
        turn_started = time.perf_counter()
        await self._ensure_session(session_id, user_id)
        before = {a["id"] for a in approvals.list_actions(session_id=session_id, limit=500)}
        notices: list[str] = []
        clean_text = egress.protect("user_in", text or "")  # personal data is redacted before anything else sees it
        if clean_text != (text or ""):
            notices.append("Personal information in your message was masked before processing.")
        parts = [types.Part(text=clean_text or "(see attached image)")]
        for data, _mime in imgs or []:
            try:
                safe, safe_mime = images_mod.sanitize_image(data)  # validate, strip metadata, downscale
            except images_mod.UnsafeImage as exc:
                audit.record("image", "INVALID_IMAGE", 1, "rejected")
                return ChatResult(reply=f"I could not use that attachment: {exc}", error="invalid_image",
                                  notices=notices)
            parts.append(types.Part.from_bytes(data=safe, mime_type=safe_mime))
        if imgs:
            notices.append("Image metadata (EXIF/GPS) was removed before analysis.")
        evidence.mark_turn(session_id)
        message = types.Content(role="user", parts=parts)

        trace: list[dict] = []
        replies: list[tuple[str, str]] = []
        error = await self._run_turn(message, session_id, user_id, trace, replies, on_event)
        reply = replies[-1][1] if replies else (
            f"Something went wrong while processing that request ({error}). Nothing was changed - please retry."
            if error else "(no reply)")
        agent = replies[-1][0] if replies else "orchestrator"

        verification = None
        if replies and not error and verify_enabled():
            reply, verification = await self._verify_and_repair(session_id, user_id, clean_text, reply, trace,
                                                                replies, on_event)
        session = await self.sessions.get_session(app_name=APP_NAME, user_id=user_id, session_id=session_id)
        degraded = list(session.state.get("degraded_notes", [])) if session else []
        new_actions = [a for a in approvals.list_actions(session_id=session_id, limit=500) if a["id"] not in before]
        reply = egress.protect("user_reply", reply)  # final gate: nothing personal reaches the user or the UI
        trace = egress.protect("user_reply", trace)
        metrics.record("chat_turn", (time.perf_counter() - turn_started) * 1000)
        return ChatResult(reply=reply, agent=agent, trace=trace, new_actions=new_actions, degraded=degraded,
                          error=error, notices=notices, verification=verification)

    async def _verify_and_repair(self, session_id, user_id, user_text, reply, trace, replies, on_event):
        """Check the answer against tool evidence + the database. If it has unsupported claims, ask the model once to
        correct itself; if it still does, strip the unverifiable sentences. Returns (final_reply, verification)."""
        started = time.perf_counter()

        def verify(text):
            return grounding.check(text, evidence.since_turn(session_id), user_text,
                                   approvals.list_actions(session_id=session_id, limit=500))

        v = verify(reply)
        initial = {"grounded": v.grounded, "hallucinated": v.hallucinated, "contradicted": v.contradicted,
                   "groundedness": v.groundedness, "issues": [i.kind for i in v.issues]}
        metrics.incr("verify_total")
        final, status = reply, "verified"
        if not v.grounded:
            metrics.incr("verify_flagged")
            problems = "; ".join(i.detail for i in v.issues if i.severity == "high" or i.kind == "contradiction")[:600]
            fix = types.Content(role="user", parts=[types.Part(text=(
                "SYSTEM CHECK (not from the user): your previous answer contained claims that tool results do not "
                f"support: {problems}. Answer the user's last question again using ONLY facts returned by tools; call "
                "tools if you need data; say you cannot confirm anything you cannot source; do not claim any action "
                "was completed (actions only become real after a human approves). Do not mention this check."))])
            more: list[tuple[str, str]] = []
            err = await self._run_turn(fix, session_id, user_id, trace, more, on_event)
            metrics.incr("verify_retried")
            retry_text = more[-1][1] if more and not err else reply
            v2 = verify(retry_text) if retry_text is not reply else v
            if v2.grounded and retry_text is not reply:
                metrics.incr("verify_repaired_by_retry")
                final, status, v = retry_text, "corrected_by_retry", v2
            else:
                metrics.incr("verify_stripped")
                final, status, v = grounding.repair(retry_text, v2.issues), "unverified_details_removed", v2
        d = v.to_dict()
        d["initial"] = initial
        d.update(status=status, retried=status != "verified",
                 grounded=(v.grounded if status == "verified" else True))  # after a fix/strip nothing unverified is shown
        metrics.record("verify", (time.perf_counter() - started) * 1000)
        return final, d

    async def close(self) -> None:
        try:
            await self.peer.aclose()
            await self.runner.close()
        except Exception:  # noqa: BLE001
            pass
