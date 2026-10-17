"""FastAPI web app: chat UI + REST API for approvals, feedback, health and reports.

Run:  python -m threadroute web      (UI at http://127.0.0.1:8000)
The chat runtime is created lazily, so the health/approvals/report endpoints work even before you
add a Gemini key; /api/chat returns a clear 503 message until credentials exist.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from threadroute import __version__
from threadroute.approvals import service as approvals
from threadroute.config import REPO_ROOT, get_settings, load_env
from threadroute.feedback import store as fb
from threadroute.feedback.report import build_report_data, build_report_markdown
from threadroute.integrations.carriers import mock_carrier_app
from threadroute.resilience import breaker, chaos, watchdog

STATIC = Path(__file__).parent / "static"
ALLOWED_IMAGES = {"image/png", "image/jpeg", "image/webp"}
MAX_IMAGE_BYTES = 8 * 1024 * 1024


class ApproveBody(BaseModel):
    comment: str | None = None
    edits: dict | None = None


class RejectBody(BaseModel):
    reason: str


class FeedbackBody(BaseModel):
    session_id: str
    message_id: str | None = None
    rating: int
    comment: str | None = None
    agent: str | None = None


class TeachBody(BaseModel):
    text: str
    scenario: str = "global"


class ChaosBody(BaseModel):
    target: str
    mode: str
    count: int | None = None
    delay_s: float = 0.0


def create_app(runtime=None) -> FastAPI:
    """`runtime` can be injected (tests use a scripted model); otherwise it is built on first chat."""
    load_env()
    app = FastAPI(title="ThreadRoute", version=__version__)
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
        rt = await get_runtime()
        images = []
        if image is not None and image.filename:
            if image.content_type not in ALLOWED_IMAGES:
                raise HTTPException(status_code=415, detail="Only png, jpeg or webp images are supported.")
            data = await image.read()
            if len(data) > MAX_IMAGE_BYTES:
                raise HTTPException(status_code=413, detail="Image is larger than 8 MB.")
            images.append((data, image.content_type))
        if not message.strip() and not images:
            raise HTTPException(status_code=400, detail="Send a message or an image.")
        session_id = session_id or f"web-{uuid.uuid4().hex[:8]}"
        result = await rt.chat(session_id, message.strip(), images)
        out = result.to_dict()
        out.update(session_id=session_id, message_id=uuid.uuid4().hex[:10])
        return out

    @app.get("/api/actions")
    def list_actions(session_id: str | None = None, status: str | None = None):
        return approvals.list_actions(session_id=session_id, status=status, limit=100)

    @app.post("/api/actions/{action_id}/approve")
    async def approve(action_id: str, body: ApproveBody):
        res = await approvals.approve(action_id, body.comment, body.edits)
        if not res["ok"]:
            raise HTTPException(status_code=409, detail=res["error"])
        return res

    @app.post("/api/actions/{action_id}/reject")
    async def reject(action_id: str, body: RejectBody):
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

    app.mount("/mock-carrier", mock_carrier_app)
    if (REPO_ROOT / "samples").exists():
        app.mount("/samples", StaticFiles(directory=str(REPO_ROOT / "samples")), name="samples")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.exception_handler(Exception)
    async def unhandled(_request, exc: Exception):  # never leak a stack trace to the browser
        return JSONResponse(status_code=500, content={"detail": f"{type(exc).__name__}: {exc}"})

    return app


app = create_app()
