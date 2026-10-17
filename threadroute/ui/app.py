"""FastAPI web app: chat UI + REST API for approvals, feedback, health and reports.

Run:  python -m threadroute web      (UI at http://127.0.0.1:8000)
The chat runtime is created lazily, so the health/approvals/report endpoints work even before you
add a Gemini key; /api/chat returns a clear 503 message until credentials exist.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import uuid
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, Form, HTTPException, Path as PathParam, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from threadroute import __version__
from threadroute.approvals import service as approvals
from threadroute.config import REPO_ROOT, get_settings, load_env
from threadroute.feedback import store as fb
from threadroute.feedback.report import build_report_data, build_report_markdown
from threadroute.guardrails import audit
from threadroute.perf import tokens as tk
from threadroute.perf.metrics import metrics
from threadroute.resilience import breaker, chaos, watchdog
from threadroute.ui import security

log = logging.getLogger("threadroute.web")

STATIC = Path(__file__).parent / "static"
ALLOWED_IMAGES = {"image/png", "image/jpeg", "image/webp"}
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_MESSAGE_CHARS = 4000
SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
ACTION_ID = PathParam(..., pattern=r"^ACT-\d{4,8}$")
_UNSET = object()


class ApproveBody(BaseModel):
    comment: str | None = Field(None, max_length=500)
    edits: dict | None = None

    @field_validator("edits")
    @classmethod
    def _small_edits(cls, v):
        if v is not None:
            if len(v) > 12 or any(not isinstance(k, str) or len(k) > 40 for k in v):
                raise ValueError("too many or too long edit fields")
            if any(isinstance(x, (dict, list)) or (isinstance(x, str) and len(x) > 300) for x in v.values()):
                raise ValueError("edit values must be short scalars")
        return v


class RejectBody(BaseModel):
    reason: str = Field(..., min_length=1, max_length=500)


class FeedbackBody(BaseModel):
    session_id: str = Field(..., pattern=r"^[A-Za-z0-9_-]{1,64}$")
    message_id: str | None = Field(None, pattern=r"^[A-Za-z0-9_-]{1,32}$")
    rating: Literal[-1, 1]
    comment: str | None = Field(None, max_length=500)
    agent: str | None = Field(None, pattern=r"^[a-z_]{1,40}$")


class TeachBody(BaseModel):
    text: str = Field(..., min_length=8, max_length=500)
    scenario: str = Field("global", pattern=r"^[a-z_]{1,40}$")


class ChaosBody(BaseModel):
    target: str = Field(..., pattern=r"^[a-z_]{1,20}:[a-z_*0-9]{1,30}$")
    mode: Literal["fail", "timeout", "slow", "malformed", "flaky"]
    count: int | None = Field(None, ge=1, le=1000)
    delay_s: float = Field(0.0, ge=0, le=30)


def resolve_auth_token() -> str | None:
    """THREADROUTE_AUTH_TOKEN=<secret> to choose one, =disabled to turn auth off (local experiments only).
    Otherwise a random token is generated for this run and printed by `python -m threadroute web`."""
    raw = os.environ.get("THREADROUTE_AUTH_TOKEN", "").strip()
    if raw.lower() == "disabled":
        return None
    return raw or secrets.token_urlsafe(24)


def create_app(runtime=None, auth_token=_UNSET) -> FastAPI:
    """`runtime` can be injected (tests use a scripted model); otherwise it is built on first chat."""
    load_env()
    debug = os.environ.get("THREADROUTE_DEBUG", "").lower() in {"1", "true", "yes"}
    app = FastAPI(title="ThreadRoute", version=__version__, docs_url="/docs" if debug else None,
                  redoc_url=None, openapi_url="/openapi.json" if debug else None)
    token = resolve_auth_token() if auth_token is _UNSET else auth_token
    app.state.auth_token = token
    hosts = ["localhost", "127.0.0.1", "[::1]", "testserver"] + [
        h.strip() for h in os.environ.get("THREADROUTE_ALLOWED_HOSTS", "").split(",") if h.strip()]
    security.install(app, token)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)  # blocks DNS-rebinding style Host headers
    state = {"runtime": runtime, "lock": asyncio.Lock()}

    async def get_runtime():
        async with state["lock"]:
            if state["runtime"] is None:
                from threadroute.agents.runtime import MissingCredentials, ThreadRouteRuntime

                try:
                    state["runtime"] = ThreadRouteRuntime()
                except MissingCredentials as exc:
                    raise HTTPException(status_code=503, detail=str(exc))
            return state["runtime"]

    @app.get("/api/status")
    def status():
        s = get_settings()
        return {"version": __version__, "model": s.model, "fallback_models": list(s.fallback_models),
                "gemini_credentials": s.has_gemini_credentials, "live_apis": s.live_apis}

    @app.post("/api/chat")
    async def chat(message: str = Form(""), session_id: str = Form(""), image: UploadFile | None = File(None)):
        if len(message) > MAX_MESSAGE_CHARS:
            raise HTTPException(status_code=413, detail=f"Message is longer than {MAX_MESSAGE_CHARS} characters.")
        if session_id and not SESSION_RE.match(session_id):
            raise HTTPException(status_code=400, detail="Invalid session id.")
        images = []
        if image is not None and image.filename:
            if image.content_type not in ALLOWED_IMAGES:
                raise HTTPException(status_code=415, detail="Only png, jpeg or webp images are supported.")
            data = await image.read(MAX_IMAGE_BYTES + 1)
            if len(data) > MAX_IMAGE_BYTES:
                raise HTTPException(status_code=413, detail="Image is larger than 8 MB.")
            images.append((data, image.content_type))
        if not message.strip() and not images:
            raise HTTPException(status_code=400, detail="Send a message or an image.")
        rt = await get_runtime()
        session_id = session_id or f"web-{uuid.uuid4().hex[:8]}"
        result = await rt.chat(session_id, message.strip(), images)
        out = result.to_dict()
        out.update(session_id=session_id, message_id=uuid.uuid4().hex[:10])
        return out

    @app.get("/api/actions")
    def list_actions(session_id: str | None = None, status: Literal["pending", "executed", "failed", "rejected"] | None = None):
        if session_id and not SESSION_RE.match(session_id):
            raise HTTPException(status_code=400, detail="Invalid session id.")
        return approvals.list_actions(session_id=session_id, status=status, limit=100)

    @app.post("/api/actions/{action_id}/approve")
    async def approve(body: ApproveBody, action_id: str = ACTION_ID):
        res = await approvals.approve(action_id, body.comment, body.edits)
        if not res["ok"]:
            raise HTTPException(status_code=409, detail=res["error"])
        return res

    @app.post("/api/actions/{action_id}/reject")
    async def reject(body: RejectBody, action_id: str = ACTION_ID):
        res = await approvals.reject(action_id, body.reason)
        if not res["ok"]:
            raise HTTPException(status_code=409, detail=res["error"])
        return res

    @app.post("/api/feedback")
    def feedback(body: FeedbackBody):
        if body.rating not in (-1, 1):
            raise HTTPException(status_code=400, detail="rating must be 1 or -1")
        fid = fb.record_feedback("thumb", body.rating, body.comment, body.session_id, "message", body.message_id,
                                 body.agent)
        learned = fb.learn_guidance(body.comment, "global", fid) if body.rating < 0 and body.comment else None
        return {"ok": True, "feedback_id": fid, "learned": learned}

    @app.post("/api/teach")
    def teach(body: TeachBody):
        return fb.learn_guidance(body.text, body.scenario)

    @app.get("/api/guidance")
    def guidance():
        return fb.list_guidance(active_only=False)

    @app.delete("/api/guidance/{guidance_id}")
    def retire(guidance_id: int):
        return {"ok": fb.delete_guidance(guidance_id)}

    @app.get("/api/health")
    def health():
        report = watchdog.health_report(60)
        report["breakers"] = breaker.all_states()
        report["chaos_rules"] = chaos.list_rules()
        report["recommendations"] = watchdog.diagnose()
        return report

    @app.post("/api/watchdog/heal")
    def heal():
        return {"applied": watchdog.auto_heal()}

    @app.post("/api/chaos")
    def set_chaos(body: ChaosBody):
        try:
            chaos.set_rule(body.target, body.mode, body.count, delay_s=body.delay_s)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"ok": True, "rules": chaos.list_rules()}

    @app.delete("/api/chaos")
    def clear_chaos():
        chaos.clear()
        return {"ok": True}

    @app.get("/api/report")
    def report():
        return {"markdown": build_report_markdown(), "data": build_report_data()}

    @app.get("/api/samples")
    def samples():
        folder = REPO_ROOT / "samples"
        return sorted(p.name for p in folder.glob("*.png")) if folder.exists() else []

    @app.get("/api/guardrails")
    def guardrails():
        return audit.summary()

    @app.get("/api/perf")
    def perf():
        return {"timing": metrics.snapshot(), "tokens": tk.ledger.snapshot()}

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    # NOTE: the mock carrier API is called in-process only; it is deliberately NOT exposed over HTTP.
    if (REPO_ROOT / "samples").exists():
        app.mount("/samples", StaticFiles(directory=str(REPO_ROOT / "samples")), name="samples")
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.exception_handler(Exception)
    async def unhandled(_request, exc: Exception):  # never leak internals or personal data to the browser
        ref = uuid.uuid4().hex[:8]
        log.error("unhandled %s ref=%s", type(exc).__name__, ref)
        return JSONResponse(status_code=500, content={"detail": f"Internal error (ref {ref})."})

    return app


app = create_app()
