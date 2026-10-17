"""Outbound email. Live mode sends through the Gmail API; otherwise (or on any failure) the message
is written to .threadroute/outbox/ so nothing is lost and the demo works with no credentials."""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from email.message import EmailMessage

from threadroute import db
from threadroute.config import get_settings
from threadroute.integrations.models import EmailResult
from threadroute.resilience.resilient import Provider, resilient_call


def _build(to: str, subject: str, body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["To"], msg["Subject"], msg["From"] = to, subject, get_settings().ops_email
    msg.set_content(body)
    return msg


def _gmail_send_sync(to: str, subject: str, body: str) -> dict:
    from googleapiclient.discovery import build

    from threadroute.integrations.google_auth import get_credentials

    service = build("gmail", "v1", credentials=get_credentials(), cache_discovery=False)
    raw = base64.urlsafe_b64encode(_build(to, subject, body).as_bytes()).decode()
    sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
    return {"message_id": sent["id"], "to": to, "subject": subject, "status": "sent", "path": None}


async def _gmail_api(to: str, subject: str, body: str) -> dict:
    return await asyncio.to_thread(_gmail_send_sync, to, subject, body)


async def _outbox(to: str, subject: str, body: str) -> dict:
    outbox = get_settings().outbox_dir
    outbox.mkdir(parents=True, exist_ok=True)
    mid = f"local-{uuid.uuid4().hex[:10]}"
    path = outbox / f"{mid}.json"
    path.write_text(json.dumps({"message_id": mid, "from": get_settings().ops_email, "to": to,
                                "subject": subject, "body": body, "created_at": db.now_iso()}, indent=2),
                    encoding="utf-8")
    return {"message_id": mid, "to": to, "subject": subject, "status": "simulated", "path": str(path)}


async def send_email(to: str, subject: str, body: str) -> dict:
    s = get_settings()
    providers = []
    if s.live_apis and s.oauth_credentials:
        providers.append(Provider("gmail_api", _gmail_api))
    live_ready = bool(s.live_apis and s.oauth_credentials)
    providers.append(Provider("outbox_file", _outbox, degraded=live_ready))
    return await resilient_call("email", providers, to, subject, body, schema=EmailResult)
