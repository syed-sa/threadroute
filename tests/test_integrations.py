import json

import pytest

from threadroute import integrations
from threadroute.config import get_settings
from threadroute.resilience import chaos, store


async def test_carrier_primary_returns_rich_validated_data():
    r = await integrations.track_shipment("SH-1001")
    assert r["source"] == "primary_api" and r["degraded"] is False
    assert r["status"] == "delayed" and r["last_location"] == "Nagpur Hub" and len(r["events"]) == 3


async def test_carrier_unknown_shipment_is_business_error_not_outage():
    r = await integrations.track_shipment("SH-9999")
    assert r["ok"] is False and r["error"]["code"] == "SHIPMENT_NOT_FOUND"
    assert not any(e["kind"] == "failure" for e in store.recent_events())


async def test_carrier_failover_chain_primary_secondary_cache():
    chaos.set_rule("carrier:primary_api", "fail")
    r = await integrations.track_shipment("SH-1001")
    assert r["source"] == "secondary_api" and r["degraded"] is False and r["events"] == []
    chaos.set_rule("carrier:secondary_api", "malformed")
    store.set_config("carrier", demoted={})
    r = await integrations.track_shipment("SH-1001")
    assert r["source"] == "cache" and r["degraded"] is True and "fallback" in r["healing_note"]


async def test_maps_models_each_mode():
    road = await integrations.estimate_route("DC-BHW", "DC-GGN", "road")
    air = await integrations.estimate_route("DC-BHW", "DC-GGN", "air")
    assert road["source"] == "route_model" and road["distance_km"] > 1000
    assert air["duration_hours"] < road["duration_hours"]
    hub = await integrations.estimate_route("Nagpur Hub", "ST-DEL-CP", "road")
    assert hub["distance_km"] > 800
    bad = await integrations.estimate_route("Atlantis", "DC-GGN", "road")
    assert bad["ok"] is False and bad["error"]["code"] == "LOCATION_NOT_FOUND"
    assert (await integrations.estimate_route("DC-BHW", "DC-GGN", "boat"))["error"]["code"] == "INVALID_MODE"
    assert (await integrations.get_weather_risk("Atlantis"))["error"]["code"] == "LOCATION_NOT_FOUND"
    assert not any(e["kind"] == "failure" for e in store.recent_events())  # user typos are not outages


async def test_maps_falls_back_to_great_circle():
    chaos.set_rule("maps:route_model", "fail")
    r = await integrations.estimate_route("DC-BHW", "DC-GGN", "road")
    assert r["source"] == "great_circle" and r["degraded"] is True


async def test_weather_levels_and_fallback():
    assert (await integrations.get_weather_risk("Nagpur Hub"))["risk_level"] == "severe"
    assert (await integrations.get_weather_risk("ST-DEL-CP"))["risk_level"] == "low"
    chaos.set_rule("weather:seasonal_model", "fail")
    r = await integrations.get_weather_risk("Nagpur Hub")
    assert r["risk_level"] == "unknown" and r["degraded"] is True


async def test_email_goes_to_outbox_file():
    r = await integrations.send_email("a@b.com", "Hello there", "Body of the message")
    assert r["status"] == "simulated"
    saved = json.loads(open(r["path"], encoding="utf-8").read())
    assert saved["to"] == "a@b.com" and saved["subject"] == "Hello there"


async def test_sheet_log_appends_csv_rows():
    await integrations.log_action("claims", ["CL-1", "SH-1002"])
    await integrations.log_action("claims", ["CL-2", "SH-1003"])
    lines = get_settings().sheet_csv.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3 and "CL-2" in lines[-1]


async def test_live_mode_without_credentials_still_works(monkeypatch):
    monkeypatch.setenv("THREADROUTE_LIVE_APIS", "true")  # no keys configured -> live providers are not offered
    assert (await integrations.estimate_route("DC-BHW", "DC-GGN", "road"))["source"] == "route_model"
    assert (await integrations.send_email("a@b.com", "Hello there", "Body of the message"))["status"] == "simulated"
