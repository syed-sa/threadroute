"""Carrier tracking.

`mock_carrier_app` is a tiny FastAPI app that behaves like a carrier-aggregator REST API. It is
called over real HTTP semantics (httpx) - in-process by default, or against CARRIER_API_BASE_URL
if you point the project at a real aggregator (you would then adapt the response mapping below).

Provider chain:  primary_api (/v1/track, rich)  ->  secondary_api (/v2/legacy, basic)
                 ->  cache (last known state from the WMS, flagged degraded)
"""

from __future__ import annotations

import httpx
from fastapi import FastAPI, HTTPException

from threadroute.config import get_settings
from threadroute.domain import wms
from threadroute.integrations.models import CarrierTracking
from threadroute.resilience.resilient import Provider, resilient_call, unavailable_as_error

mock_carrier_app = FastAPI(title="Mock carrier aggregator")


def _events(s: dict) -> list[dict]:
    ev = [{"ts": s["updated_at"], "location": s["origin"], "description": f"Picked up from {s['origin']}"}]
    if s["status"] != "pending_pickup":
        ev.append({"ts": s["updated_at"], "location": s["last_location"], "description": s["last_event"]})
    if s["hold_reason"]:
        ev.append({"ts": s["updated_at"], "location": s["last_location"], "description": f"Exception: {s['hold_reason']}"})
    return ev


@mock_carrier_app.get("/v1/track/{shipment_id}")
def track_v1(shipment_id: str):
    try:
        s = wms.get_shipment(shipment_id)
    except wms.WmsError as exc:
        raise HTTPException(status_code=404, detail=exc.message)
    return {"shipment_id": s["shipment_id"], "carrier": s["carrier"], "status": s["status"], "eta": s["eta"],
            "last_location": s["last_location"], "last_event": s["last_event"],
            "hold_reason": s["hold_reason"], "events": _events(s)}


@mock_carrier_app.get("/v2/legacy/{shipment_id}")
def track_v2(shipment_id: str):
    try:
        s = wms.get_shipment(shipment_id)
    except wms.WmsError as exc:
        raise HTTPException(status_code=404, detail=exc.message)
    return {"shipment_id": s["shipment_id"], "carrier": s["carrier"], "status": s["status"], "eta": s["eta"],
            "last_location": s["last_location"]}


def _client() -> httpx.AsyncClient:
    base = get_settings().carrier_api_base_url
    if base:
        return httpx.AsyncClient(base_url=base, timeout=10)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=mock_carrier_app), base_url="http://carrier.local")


async def _get(path: str) -> dict:
    async with _client() as client:
        resp = await client.get(path)
        if resp.status_code == 404:
            raise LookupError(resp.json().get("detail", "not found"))
        resp.raise_for_status()
        return resp.json()


async def _primary(shipment_id: str) -> dict:
    return await _get(f"/v1/track/{shipment_id}")


async def _secondary(shipment_id: str) -> dict:
    return await _get(f"/v2/legacy/{shipment_id}")


async def _cache(shipment_id: str) -> dict:
    s = wms.get_shipment(shipment_id)
    return {"shipment_id": s["shipment_id"], "carrier": s["carrier"], "status": s["status"], "eta": s["eta"],
            "last_location": s["last_location"], "last_event": s["last_event"], "hold_reason": s["hold_reason"]}


@unavailable_as_error
async def track_shipment(shipment_id: str) -> dict:
    shipment_id = shipment_id.strip().upper()
    try:
        wms.get_shipment(shipment_id)  # fail fast on unknown ids (a business error, not an outage)
    except wms.WmsError as exc:
        return exc.to_dict()
    return await resilient_call(
        "carrier",
        [Provider("primary_api", _primary), Provider("secondary_api", _secondary),
         Provider("cache", _cache, degraded=True)],
        shipment_id, schema=CarrierTracking,
    )
