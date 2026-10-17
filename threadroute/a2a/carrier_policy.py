"""The CARRIER side's own business rules (its data and policy are separate from ThreadRoute's).

These are plain deterministic functions used as tools by the carrier-desk agent, and as the local fallback
when the remote carrier agent is unreachable (answers are then flagged degraded)."""

from __future__ import annotations

import hashlib
from datetime import date, timedelta

from threadroute.domain import constants as C
from threadroute.domain import wms
from threadroute.integrations.maps import resolve_point
from threadroute.data.seed import haversine_km as _hav

ACCEPTANCE = {"minor": 0.40, "moderate": 0.70, "severe": 0.90}
CLAIM_WINDOW_DAYS = 14
SPEED_KMH = {"road": 55.0, "rail": 40.0, "air": 600.0}


def _ref(*parts: str) -> str:
    return "CARR-" + hashlib.sha256("|".join(parts).encode()).hexdigest()[:8].upper()


def carrier_shipment_status(shipment_id: str) -> dict:
    """The carrier's own view of a shipment: status, position, ETA, hold reason, handling notes."""
    try:
        s = wms.get_shipment(shipment_id)
    except wms.WmsError as exc:
        return exc.to_dict()
    return {"ok": True, "shipment_id": s["shipment_id"], "carrier": s["carrier"], "mode": s["mode"],
            "status": s["status"], "position": s["last_location"], "eta": s["eta"],
            "hold_reason": s["hold_reason"], "handling": s["handling"], "units": s["total_units"]}


def carrier_quote_reroute(shipment_id: str, mode: str) -> dict:
    """A binding-for-24h quote to move the remaining journey of a shipment by `mode` (road|rail|air)."""
    try:
        s = wms.get_shipment(shipment_id)
    except wms.WmsError as exc:
        return exc.to_dict()
    mode = (mode or "").lower()
    offers = [(name, cfg) for name, cfg in C.CARRIERS.items() if mode in cfg["modes"]]
    if not offers:
        return {"ok": False, "error": {"code": "MODE_NOT_OFFERED", "message": f"No carrier offers '{mode}'."}}
    if s["status"] in ("delivered", "cancelled"):
        return {"ok": False, "error": {"code": "NOT_REROUTABLE", "message": f"{s['shipment_id']} is {s['status']}."}}
    here = f"{s['last_lat']},{s['last_lon']}" if s.get("last_lat") else s["origin_id"]
    _, la, lo = resolve_point(here)
    _, da, do = resolve_point(s["destination_id"])
    km = _hav(la, lo, da, do) * (1.25 if mode != "air" else 1.05)
    weight = sum(i["qty"] * C.UNIT_WEIGHT_KG[wms.get_sku(i["sku"])["category"]] for i in s["items"])
    hours = km / SPEED_KMH[mode] + 12 + (8 if mode == "air" else 0)
    name, cfg = min(offers, key=lambda o: o[1]["rate_per_kg_km"][mode])
    return {"ok": True, "reference": _ref(shipment_id, mode), "carrier": name, "mode": mode,
            "distance_km": round(km), "transit_hours": round(hours, 1),
            "est_arrival": (date.today() + timedelta(days=int(hours // 24) + 1)).isoformat(),
            "price_inr": round(weight * km * cfg["rate_per_kg_km"][mode] * 1.05),
            "valid_until": (date.today() + timedelta(days=1)).isoformat()}


def carrier_assess_claim(shipment_id: str, claim_type: str, severity: str, affected_units: int,
                         evidence_provided: bool = False) -> dict:
    """Pre-assess a claim under the carrier's policy: required evidence, acceptance level and indicative payout."""
    try:
        s = wms.get_shipment(shipment_id)
    except wms.WmsError as exc:
        return exc.to_dict()
    claim_type, severity = (claim_type or "").lower(), (severity or "").lower()
    if claim_type not in C.CLAIM_TYPES or severity not in ACCEPTANCE:
        return {"ok": False, "error": {"code": "INVALID_INPUT",
                                       "message": f"claim_type in {sorted(C.CLAIM_TYPES)}, severity in {sorted(ACCEPTANCE)}."}}
    units = max(0, min(int(affected_units), s["total_units"]))
    avg_cost = sum((i["unit_cost"] or 0) * i["qty"] for i in s["items"]) / max(1, s["total_units"])
    requirements = []
    if not evidence_provided:
        requirements.append("photo evidence of the damage / signed packing list for shortages")
    if claim_type in ("damage", "shortage") and s["status"] != "delivered":
        requirements.append("proof of delivery - claims open only after delivery")
    requirements.append(f"file within {CLAIM_WINDOW_DAYS} days of delivery")
    pct = ACCEPTANCE[severity] if claim_type != "loss" else 1.0
    if claim_type == "loss":
        requirements.append("14-day tracing investigation before settlement")
    decision = "accepted_in_principle" if evidence_provided and s["status"] == "delivered" else "needs_evidence"
    return {"ok": True, "reference": _ref(shipment_id, claim_type, severity), "decision": decision,
            "settlement_percent": round(pct * 100), "indicative_payout_inr": round(avg_cost * units * pct, 2),
            "units_considered": units, "requirements": requirements, "carrier": s["carrier"]}
