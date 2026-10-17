"""Action log. Live mode appends rows to a Google Sheet; otherwise (or on failure) to a local CSV."""

from __future__ import annotations

import asyncio
import csv

from threadroute import db
from threadroute.config import get_settings
from threadroute.guardrails import egress
from threadroute.integrations.models import SheetResult
from threadroute.resilience.resilient import Provider, resilient_call


def _sheets_append_sync(tab: str, values: list[str]) -> dict:
    from googleapiclient.discovery import build

    from threadroute.integrations.google_auth import get_credentials

    s = get_settings()
    service = build("sheets", "v4", credentials=get_credentials(), cache_discovery=False)
    service.spreadsheets().values().append(
        spreadsheetId=s.sheet_id, range=f"{tab}!A1", valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS", body={"values": [[db.now_iso(), *values]]}).execute()
    return {"tab": tab, "status": "appended", "location": f"sheet:{s.sheet_id}/{tab}"}


async def _sheets_api(tab: str, values: list[str]) -> dict:
    return await asyncio.to_thread(_sheets_append_sync, tab, values)


async def _csv(tab: str, values: list[str]) -> dict:
    path = get_settings().sheet_csv
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if new:
            writer.writerow(["timestamp", "tab", "col1", "col2", "col3", "col4", "col5"])
        writer.writerow([db.now_iso(), tab, *values])
    return {"tab": tab, "status": "logged_locally", "location": str(path)}


async def log_action(tab: str, values: list[str]) -> dict:
    values = egress.protect("sheets", [str(v) for v in values])
    s = get_settings()
    providers = []
    if s.live_apis and s.oauth_credentials and s.sheet_id:
        providers.append(Provider("sheets_api", _sheets_api))
    providers.append(Provider("csv_log", _csv))
    return await resilient_call("sheets", providers, tab, [str(v) for v in values], schema=SheetResult)
