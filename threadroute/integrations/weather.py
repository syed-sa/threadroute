"""Weather risk along a lane. Live mode uses Open-Meteo (free, no key); offline mode uses a
deterministic seasonal model so demos are repeatable. A last-resort default flags 'unknown'."""

from __future__ import annotations

from datetime import date, timedelta

import httpx

from threadroute.config import get_settings
from threadroute.integrations.maps import resolve_point
from threadroute.integrations.models import WeatherRisk
from threadroute.resilience.resilient import Provider, resilient_call, unavailable_as_error

OPEN_METEO = "https://api.open-meteo.com/v1/forecast"

# Offline "forecast" - rain-affected corridors for the demo (October monsoon retreat).
_MOCK_PRECIP = {"nagpur hub": 120, "indore hub": 40, "mumbai": 45, "bhiwandi": 55, "chennai": 70,
                "pune": 25, "hyderabad": 35, "bengaluru": 22, "kolkata": 30, "delhi": 2, "gurugram": 2,
                "jaipur hub": 4, "delhi ncr": 2}


def _level(precip: float, wind: float) -> str:
    if precip >= 100 or wind >= 80:
        return "severe"
    if precip >= 50 or wind >= 60:
        return "high"
    if precip >= 20 or wind >= 40:
        return "moderate"
    return "low"


def _summary(level: str, loc: str, precip: float, wind: float) -> str:
    return (f"{level.upper()} weather risk at {loc}: up to {precip:.0f} mm rain/day and "
            f"{wind:.0f} km/h wind in the next days.")


async def _open_meteo(location: str, days: int) -> dict:
    name, lat, lon = resolve_point(location)
    params = {"latitude": lat, "longitude": lon, "daily": "precipitation_sum,wind_speed_10m_max",
              "forecast_days": max(1, min(days, 7)), "timezone": "auto"}
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(OPEN_METEO, params=params)
        resp.raise_for_status()
        daily = resp.json()["daily"]
    rows = [{"date": d, "precipitation_mm": p or 0, "wind_kmh": w or 0}
            for d, p, w in zip(daily["time"], daily["precipitation_sum"], daily["wind_speed_10m_max"])]
    mp, mw = max(r["precipitation_mm"] for r in rows), max(r["wind_kmh"] for r in rows)
    level = _level(mp, mw)
    return {"location": name, "risk_level": level, "max_precipitation_mm": mp, "max_wind_kmh": mw,
            "summary": _summary(level, name, mp, mw) + " (Open-Meteo)", "days": rows}


async def _mock(location: str, days: int) -> dict:
    name, _lat, _lon = resolve_point(location)
    key = next((k for k in _MOCK_PRECIP if k in name.lower()), None)
    if key is None:
        key = next((k for k in _MOCK_PRECIP if k in location.lower()), "delhi")
    peak = _MOCK_PRECIP[key]
    rows = [{"date": (date.today() + timedelta(days=i)).isoformat(),
             "precipitation_mm": round(peak * f, 1), "wind_kmh": round(20 + peak * 0.3 * f, 1)}
            for i, f in enumerate([1.0, 0.8, 0.5, 0.3, 0.2][: max(1, min(days, 5))])]
    mp, mw = max(r["precipitation_mm"] for r in rows), max(r["wind_kmh"] for r in rows)
    level = _level(mp, mw)
    return {"location": name, "risk_level": level, "max_precipitation_mm": mp, "max_wind_kmh": mw,
            "summary": _summary(level, name, mp, mw) + " (offline seasonal model)", "days": rows}


async def _default(location: str, days: int) -> dict:
    name, *_ = resolve_point(location)
    return {"location": name, "risk_level": "unknown", "max_precipitation_mm": 0, "max_wind_kmh": 0,
            "summary": f"No forecast available for {name}; treat weather risk as unknown.", "days": []}


@unavailable_as_error
async def get_weather_risk(location: str, days: int = 3) -> dict:
    try:  # bad user input is a business error - it must not count as a provider failure
        resolve_point(location)
    except LookupError as exc:
        return {"ok": False, "error": {"code": "LOCATION_NOT_FOUND", "message": str(exc)}}
    providers = []
    if get_settings().live_apis:
        providers.append(Provider("open_meteo", _open_meteo))
    providers += [Provider("seasonal_model", _mock), Provider("unknown_default", _default, degraded=True)]
    return await resilient_call("weather", providers, location, days, schema=WeatherRisk)
