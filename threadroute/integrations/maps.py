"""Route estimation. Live mode uses the Google Maps Routes API for road legs; everything else
(air, rail, offline mode, or an outage) uses a transport model, then a plain great-circle estimate."""

from __future__ import annotations

import httpx

from threadroute.config import get_settings
from threadroute.data.seed import haversine_km
from threadroute.domain import wms
from threadroute.integrations.models import RouteEstimate
from threadroute.resilience.resilient import Provider, resilient_call, unavailable_as_error

ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
SPEED_KMH = {"road": 55.0, "rail": 40.0, "air": 600.0}
HANDLING_HOURS = {"road": 0.0, "rail": 12.0, "air": 8.0}  # loading/unloading/airport cut-off allowance
ROAD_FACTOR = {"road": 1.28, "rail": 1.2, "air": 1.0}


def resolve_point(ref: str) -> tuple[str, float, float]:
    """Accepts a location id (ST-DEL-CP), a 'lat,lon' pair, or a known hub name (Nagpur Hub)."""
    from threadroute.data.seed import WAYPOINTS

    ref = ref.strip()
    for loc in wms.list_locations():
        if ref.lower() in (loc["location_id"].lower(), loc["city"].lower(), loc["name"].lower()):
            return loc["name"], loc["lat"], loc["lon"]
    for name, (lat, lon) in WAYPOINTS.items():
        if ref.lower() == name.lower():
            return name, lat, lon
    try:
        lat_s, lon_s = ref.split(",")
        return ref, float(lat_s), float(lon_s)
    except ValueError:
        raise LookupError(f"Cannot resolve location '{ref}'. Use a location id, hub name or 'lat,lon'.")


async def _google(origin: str, destination: str, mode: str) -> dict:
    s = get_settings()
    o, d = resolve_point(origin), resolve_point(destination)
    body = {
        "origin": {"location": {"latLng": {"latitude": o[1], "longitude": o[2]}}},
        "destination": {"location": {"latLng": {"latitude": d[1], "longitude": d[2]}}},
        "travelMode": "DRIVE", "routingPreference": "TRAFFIC_AWARE",
    }
    headers = {"X-Goog-Api-Key": s.maps_api_key or "", "X-Goog-FieldMask": "routes.duration,routes.distanceMeters"}
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(ROUTES_URL, json=body, headers=headers)
        resp.raise_for_status()
        route = resp.json()["routes"][0]
    return {"origin": o[0], "destination": d[0], "mode": "road",
            "distance_km": round(route["distanceMeters"] / 1000, 1),
            "duration_hours": round(int(route["duration"].rstrip("s")) / 3600, 1),
            "note": "Google Maps Routes API (traffic-aware)"}


async def _model(origin: str, destination: str, mode: str) -> dict:
    o, d = resolve_point(origin), resolve_point(destination)
    km = haversine_km(o[1], o[2], d[1], d[2]) * ROAD_FACTOR[mode]
    hours = km / SPEED_KMH[mode] + HANDLING_HOURS[mode]
    return {"origin": o[0], "destination": d[0], "mode": mode, "distance_km": round(km, 1),
            "duration_hours": round(max(hours, 0.5), 1), "note": "Transport model estimate"}


async def _great_circle(origin: str, destination: str, mode: str) -> dict:
    o, d = resolve_point(origin), resolve_point(destination)
    km = haversine_km(o[1], o[2], d[1], d[2])
    return {"origin": o[0], "destination": d[0], "mode": mode, "distance_km": round(max(km, 1), 1),
            "duration_hours": round(max(km / 45, 0.5), 1), "note": "Great-circle fallback (rough)"}


@unavailable_as_error
async def estimate_route(origin: str, destination: str, mode: str = "road") -> dict:
    mode = mode.lower()
    if mode not in SPEED_KMH:
        return {"ok": False, "error": {"code": "INVALID_MODE", "message": "mode must be road, rail or air"}}
    try:  # bad user input is a business error - it must not count as a provider failure
        resolve_point(origin)
        resolve_point(destination)
    except LookupError as exc:
        return {"ok": False, "error": {"code": "LOCATION_NOT_FOUND", "message": str(exc)}}
    s = get_settings()
    providers = []
    if s.live_apis and s.maps_api_key and mode == "road":
        providers.append(Provider("google_routes", _google))
    providers += [Provider("route_model", _model), Provider("great_circle", _great_circle, degraded=True)]
    return await resilient_call("maps", providers, origin, destination, mode, schema=RouteEstimate)
