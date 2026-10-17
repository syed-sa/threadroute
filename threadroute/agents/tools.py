"""Local (non-MCP) tools for the agents.

* Integration tools wrap the resilient external API layer (carrier, maps, weather).
* propose_* tools never change anything: they validate, then queue a pending action for a person.
* record/get_image_findings carry structured results of photo analysis between agents via session state.
* Watchdog tools expose the self-healing controls.
"""

from __future__ import annotations

import json
import math
from typing import Literal

from google.adk.tools.tool_context import ToolContext
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from threadroute import integrations
from threadroute.approvals import service as approvals
from threadroute.approvals.models import PAYLOAD_MODELS
from threadroute.domain import constants as C
from threadroute.domain import wms
from threadroute.feedback import store as fb
from threadroute.resilience import chaos, watchdog


# ------------------------------------------------------------------ integration tools
async def get_carrier_tracking(shipment_id: str) -> dict:
    """Live carrier tracking for a shipment (status, ETA, last location, hold reason, event history).
    Calls the carrier API with automatic retry and fallback; check the 'degraded' and 'healing_note'
    fields - if degraded is true the data came from a fallback source and may be stale."""
    return await integrations.track_shipment(shipment_id)


async def estimate_route(origin: str, destination: str, mode: str = "road") -> dict:
    """Estimate distance and transit hours between two places. origin/destination can be a location id
    (e.g. DC-BHW, ST-DEL-CP), a hub name (e.g. 'Nagpur Hub') or 'lat,lon'. mode is road, rail or air."""
    return await integrations.estimate_route(origin, destination, mode)


async def get_weather_risk(location: str, days: int = 3) -> dict:
    """Weather risk (low/moderate/high/severe/unknown) for a location id, city or hub name over the next days."""
    return await integrations.get_weather_risk(location, days)


async def get_reroute_options(shipment_id: str) -> dict:
    """Compare the current plan with every alternative carrier/mode for an undelivered shipment:
    estimated arrival, transit hours, freight cost (INR), whether it meets the promised date and whether
    road/rail legs are exposed to bad weather at the shipment's current position."""
    try:
        s = wms.get_shipment(shipment_id)
    except wms.WmsError as exc:
        return exc.to_dict()
    if s["status"] in ("delivered", "cancelled"):
        return {"ok": False, "error": {"code": "NOT_REROUTABLE", "message": f"{s['shipment_id']} is {s['status']}."}}
    weight = 0.0
    for item in s["items"]:
        weight += item["qty"] * C.UNIT_WEIGHT_KG[wms.get_sku(item["sku"])["category"]]
    here = f"{s['last_lat']},{s['last_lon']}" if s.get("last_lat") else s["origin_id"]
    weather = await integrations.get_weather_risk(s["last_location"] or s["origin_id"])
    tracking = await integrations.track_shipment(s["shipment_id"])
    from datetime import date, timedelta
    promised = date.fromisoformat(s["promised_date"])
    options = []
    for carrier, cfg in C.CARRIERS.items():
        for mode in cfg["modes"]:
            if carrier == s["carrier"] and mode == s["mode"]:
                continue
            route = await integrations.estimate_route(here, s["destination_id"], mode)
            if route.get("ok") is False:
                continue
            hours = route["duration_hours"] + 12  # re-booking / pickup lead time
            arrival = date.today() + timedelta(days=math.ceil(hours / 24))
            options.append({
                "carrier": carrier, "mode": mode, "est_transit_hours": round(hours, 1),
                "est_arrival_date": arrival.isoformat(),
                "est_cost_inr": round(weight * route["distance_km"] * cfg["rate_per_kg_km"][mode]),
                "meets_promised_date": arrival <= promised,
                "exposed_to_weather_risk": mode != "air" and weather.get("risk_level") in ("high", "severe"),
            })
    options.sort(key=lambda o: o["est_arrival_date"])
    return {
        "shipment_id": s["shipment_id"], "units": s["total_units"], "approx_weight_kg": round(weight),
        "promised_date": s["promised_date"], "handling": s["handling"],
        "current_plan": {"carrier": s["carrier"], "mode": s["mode"], "status": tracking.get("status"),
                         "eta": tracking.get("eta"), "hold_reason": tracking.get("hold_reason"),
                         "tracking_source": tracking.get("source"), "tracking_degraded": tracking.get("degraded")},
        "weather_at_current_position": {"location": weather.get("location") or s["last_location"],
                                        "risk_level": weather.get("risk_level") or "unknown",
                                        "summary": weather.get("summary") or "Weather data unavailable."},
        "alternatives": options,
    }


# ------------------------------------------------------------------ image findings
class ImageFindings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    image_type: Literal["damaged_carton", "packing_list", "shipping_label", "invoice", "returned_garment", "other"]
    summary: str = Field(min_length=10)
    shipment_id: str = ""
    po_id: str = ""
    return_id: str = ""
    damage_type: Literal["crushed_carton", "torn_carton", "water_damage", "stained_garments",
                         "creased_hanging", "seal_broken", "other", "none"] = "none"
    severity: Literal["minor", "moderate", "severe"] = "minor"
    affected_units: int = Field(default=0, ge=0)
    listed_units: int = Field(default=0, ge=0)
    garment_condition: str = ""
    visible_text: str = ""


def record_image_findings(image_type: str, summary: str, shipment_id: str = "", po_id: str = "",
                          return_id: str = "", damage_type: str = "none", severity: str = "minor",
                          affected_units: int = 0, listed_units: int = 0, garment_condition: str = "",
                          visible_text: str = "", tool_context: ToolContext = None) -> dict:
    """Save what you observed in an attached image so other agents can act on it. image_type is one of
    damaged_carton, packing_list, shipping_label, invoice, returned_garment, other. Fill only what is
    actually visible; use 0 / empty string for unknown. listed_units is the quantity printed on a packing
    list. affected_units is how many units you estimate are damaged."""
    try:
        findings = ImageFindings(
            image_type=image_type, summary=summary, shipment_id=shipment_id.upper(), po_id=po_id.upper(),
            return_id=return_id.upper(), damage_type=damage_type, severity=severity, affected_units=affected_units,
            listed_units=listed_units, garment_condition=garment_condition, visible_text=visible_text)
    except ValidationError as exc:
        return {"status": "invalid", "errors": _errors(exc),
                "fix": "Correct the listed fields (use the allowed enum values) and call the tool again."}
    stored = tool_context.state.get("image_findings", []) if tool_context else []
    if tool_context:
        tool_context.state["image_findings"] = [*stored, findings.model_dump()]
    return {"status": "recorded", "findings": findings.model_dump(),
            "next_step": "Verify any ids with get_shipment/get_return, then hand off to the right specialist."}


def get_image_findings(tool_context: ToolContext = None) -> dict:
    """Return the structured findings recorded earlier from images in this conversation (newest last)."""
    findings = tool_context.state.get("image_findings", []) if tool_context else []
    return {"count": len(findings), "findings": findings}


# ------------------------------------------------------------------ proposals (human-approved)
def _errors(exc: ValidationError) -> list[str]:
    return [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]


def _precheck(kind: str, p: dict) -> list[str]:
    errs: list[str] = []
    try:
        if kind == "reallocate_stock":
            if p["from_location"] == p["to_location"]:
                errs.append("from_location and to_location must differ.")
            else:
                wms.transferable_surplus(p["to_location"], p["sku"], p["size"])  # destination must stock the item
                surplus = wms.transferable_surplus(p["from_location"], p["sku"], p["size"])
                if p["qty"] > surplus:
                    errs.append(f"{p['from_location']} can release at most {surplus} units of {p['sku']}/{p['size']} "
                                "while keeping its safety cover. Lower qty or choose another donor.")
        elif kind == "file_claim":
            s = wms.get_shipment(p["shipment_id"])
            if p["affected_units"] > s["total_units"]:
                errs.append(f"affected_units cannot exceed the shipment's {s['total_units']} units.")
            dup = [c["claim_id"] for c in wms.list_claims(s["shipment_id"]) if c["claim_type"] == p["claim_type"]]
            if dup:
                errs.append(f"A {p['claim_type']} claim already exists for {s['shipment_id']}: {', '.join(dup)}.")
        elif kind == "reroute_shipment":
            s = wms.get_shipment(p["shipment_id"])
            if s["status"] in ("delivered", "cancelled"):
                errs.append(f"{s['shipment_id']} is already {s['status']} and cannot be rerouted.")
            if p["new_mode"] not in C.CARRIERS[p["new_carrier"]]["modes"]:
                errs.append(f"{p['new_carrier']} does not offer {p['new_mode']} (offers {C.CARRIERS[p['new_carrier']]['modes']}).")
            if (p["new_carrier"], p["new_mode"]) == (s["carrier"], s["mode"]):
                errs.append("That is already the current carrier and mode.")
        elif kind == "set_return_disposition":
            r = wms.get_return(p["return_id"])
            if r["status"] == "graded":
                errs.append(f"{r['return_id']} is already graded ({r['grade']}/{r['disposition']}).")
            if p["disposition"] not in C.GRADE_DISPOSITIONS[p["grade"]]:
                errs.append(f"Grade {p['grade']} allows {sorted(C.GRADE_DISPOSITIONS[p['grade']])}, not '{p['disposition']}'.")
    except wms.WmsError as exc:
        errs.append(exc.message)
    return errs


def _propose(tool_context: ToolContext, kind: str, payload: dict, summary: str, rationale: str = "") -> dict:
    try:
        clean = PAYLOAD_MODELS[kind].model_validate(payload).model_dump()
    except ValidationError as exc:
        return {"status": "invalid", "errors": _errors(exc),
                "fix": "Correct the listed fields and call the tool again. Nothing was queued."}
    errs = _precheck(kind, clean)
    if errs:
        return {"status": "invalid", "errors": errs, "fix": "Adjust the proposal and call the tool again. Nothing was queued."}
    session_id = tool_context.session.id if tool_context else "unknown"
    for existing in approvals.list_actions(session_id=session_id, status="pending"):
        if existing["kind"] == kind and existing["payload"] == clean:
            return {"status": "pending_approval", "action_id": existing["id"], "summary": existing["summary"],
                    "note": "An identical proposal is already awaiting approval."}
    scenario = fb.KIND_TO_SCENARIO[kind]
    rules = fb.retrieve([scenario], f"{summary} {rationale} {json.dumps(clean)}")
    action = approvals.create(session_id, tool_context.agent_name if tool_context else "agent", kind, clean,
                              summary, rationale, [r["id"] for r in rules])
    return {
        "status": "pending_approval", "action_id": action["id"], "summary": summary,
        "applied_guidance": [r["text"] for r in rules],
        "instruction": (f"Tell the user {action['id']} is waiting for their approval (Approve/Reject in the UI, or "
                        f"'/approve {action['id']}' in the terminal). Do NOT say it has been done."),
    }


def propose_reallocation(from_location: str, to_location: str, sku: str, size: str, qty: int,
                         rationale: str = "", tool_context: ToolContext = None) -> dict:
    """Queue a stock transfer between two locations for human approval (does not move stock itself).
    Location ids look like ST-BLR-IND or DC-GGN. qty must not exceed the donor's transferable surplus."""
    return _propose(tool_context, "reallocate_stock",
                    {"from_location": from_location, "to_location": to_location, "sku": sku, "size": str(size),
                     "qty": qty, "reason": rationale or "stock rebalancing"},
                    f"Move {qty} x {sku} size {size} from {from_location} to {to_location}", rationale)


def propose_file_claim(shipment_id: str, claim_type: str, damage_type: str, severity: str, affected_units: int,
                       evidence_summary: str, tool_context: ToolContext = None) -> dict:
    """Queue a carrier claim for human approval. claim_type: damage|shortage|loss. damage_type: crushed_carton|
    torn_carton|water_damage|stained_garments|creased_hanging|seal_broken|other|none. severity: minor|moderate|severe.
    On approval the claim is filed, the carrier is emailed and the action is logged."""
    return _propose(tool_context, "file_claim",
                    {"shipment_id": shipment_id.upper(), "claim_type": claim_type, "damage_type": damage_type,
                     "severity": severity, "affected_units": affected_units, "evidence_summary": evidence_summary},
                    f"File {claim_type} claim on {shipment_id.upper()} ({affected_units} units, {severity})",
                    evidence_summary)


def propose_reroute(shipment_id: str, new_mode: str, new_carrier: str, reason: str, new_eta: str = "",
                    tool_context: ToolContext = None) -> dict:
    """Queue a reroute for human approval. new_carrier: SwiftShip|RoadRunner Express|AirLink Cargo and new_mode
    must be offered by that carrier (SwiftShip: road/rail, RoadRunner Express: road, AirLink Cargo: air).
    Use get_reroute_options first. new_eta is an ISO date from that tool (optional)."""
    return _propose(tool_context, "reroute_shipment",
                    {"shipment_id": shipment_id.upper(), "new_mode": new_mode, "new_carrier": new_carrier,
                     "reason": reason, "new_eta": new_eta or None},
                    f"Reroute {shipment_id.upper()} via {new_carrier} ({new_mode})", reason)


def propose_return_disposition(return_id: str, grade: str, disposition: str, notes: str = "",
                               tool_context: ToolContext = None) -> dict:
    """Queue a return grade (A-D) and disposition (restock|refurbish|liquidate|recycle) for approval.
    A->restock, B->refurbish or restock, C->liquidate or refurbish, D->recycle or liquidate."""
    return _propose(tool_context, "set_return_disposition",
                    {"return_id": return_id.upper(), "grade": grade.upper(), "disposition": disposition, "notes": notes},
                    f"Grade {return_id.upper()} as {grade.upper()} and {disposition}", notes)


def propose_email(to: str, subject: str, body: str, tool_context: ToolContext = None) -> dict:
    """Queue an email (to a store, carrier or supplier) for human approval. Never invent addresses: use
    store.<location-id-lowercase>@aurelio-menswear.example.com for stores, or ones the user supplied."""
    return _propose(tool_context, "send_email", {"to": to, "subject": subject, "body": body},
                    f"Email {to}: {subject}", "")


# ------------------------------------------------------------------ watchdog tools
def get_health_report(window_minutes: int = 60) -> dict:
    """Self-healing health report: per-provider call counts, failure rates, latency, open circuit breakers,
    active configuration and recent healing events."""
    return watchdog.health_report(window_minutes)


def diagnose_failures(window_minutes: int = 30) -> dict:
    """Diagnose unhealthy providers and recommend remedies (does not apply them)."""
    return {"recommendations": watchdog.diagnose(window_minutes)}


def apply_remedy(component: str, action: str, provider: str = "", seconds: int = 120, timeout: float = 0,
                 max_retries: int = 0) -> dict:
    """Apply one remedy. action: demote_provider|restore_provider|disable_provider|enable_provider|
    increase_timeout|increase_retries|reset_breaker. component: carrier|maps|weather|email|sheets|mcp.
    provider is required for provider-level actions (e.g. primary_api)."""
    params = {k: v for k, v in {"seconds": seconds, "timeout": timeout or None, "max_retries": max_retries or None}.items() if v}
    return watchdog.apply_remedy(component, action, provider or None, params)


def run_auto_heal() -> dict:
    """Diagnose and apply all recommended remedies in one step."""
    return {"applied": watchdog.auto_heal()}


def get_chaos_rules() -> dict:
    """List any failure-injection rules currently active (used for resilience drills)."""
    return {"rules": chaos.list_rules()}
