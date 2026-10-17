"""Chat runtime: wraps the ADK Runner and returns a simple, UI-friendly ChatResult."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from threadroute.agents.factory import build_root_agent
from threadroute.approvals import service as approvals
from threadroute.config import get_settings
from threadroute.domain import wms
from threadroute.feedback import store as fb
from threadroute.resilience import store as rstore

APP_NAME = "threadroute"


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
        fb.ensure()
        self.root = build_root_agent(model)
        self.sessions = InMemorySessionService()
        self.runner = Runner(agent=self.root, app_name=APP_NAME, session_service=self.sessions)

    async def _ensure_session(self, session_id: str, user_id: str):
        session = await self.sessions.get_session(app_name=APP_NAME, user_id=user_id, session_id=session_id)
        if session is None:
            await self.sessions.create_session(app_name=APP_NAME, user_id=user_id, session_id=session_id)

    async def chat(self, session_id: str, text: str, images: list[tuple[bytes, str]] | None = None,
                   user_id: str = "ops") -> ChatResult:
        await self._ensure_session(session_id, user_id)
        before = {a["id"] for a in approvals.list_actions(session_id=session_id, limit=500)}
        parts = [types.Part(text=text or "(see attached image)")]
        for data, mime in images or []:
            parts.append(types.Part.from_bytes(data=data, mime_type=mime))
        message = types.Content(role="user", parts=parts)

        trace: list[dict] = []
        replies: list[tuple[str, str]] = []
        error = None
        try:
            async for event in self.runner.run_async(user_id=user_id, session_id=session_id, new_message=message):
                for call in event.get_function_calls():
                    kind = "transfer" if call.name == "transfer_to_agent" else "tool_call"
                    trace.append({"type": kind, "agent": event.author, "name": call.name, "args": _short(dict(call.args or {}))})
                for resp in event.get_function_responses():
                    trace.append({"type": "tool_result", "agent": event.author, "name": resp.name,
                                  "result": _short(resp.response)})
                if event.content and event.content.parts and not event.partial and event.is_final_response():
                    text_out = "".join(p.text for p in event.content.parts if p.text and not getattr(p, "thought", False))
                    if text_out.strip():
                        replies.append((event.author, text_out.strip()))
        except Exception as exc:  # noqa: BLE001 - a chat turn must never crash the UI/CLI
            error = f"{type(exc).__name__}: {exc}"
            rstore.log_event("agent_runtime", None, "failure", error[:300])
        reply = replies[-1][1] if replies else (
            f"Something went wrong while processing that request ({error}). Nothing was changed - please retry."
            if error else "(no reply)")
        agent = replies[-1][0] if replies else "orchestrator"
        session = await self.sessions.get_session(app_name=APP_NAME, user_id=user_id, session_id=session_id)
        degraded = list(session.state.get("degraded_notes", [])) if session else []
        new_actions = [a for a in approvals.list_actions(session_id=session_id, limit=500) if a["id"] not in before]
        return ChatResult(reply=reply, agent=agent, trace=trace, new_actions=new_actions, degraded=degraded, error=error)

    async def close(self) -> None:
        try:
            await self.runner.close()
        except Exception:  # noqa: BLE001
            pass
