"""Warehouse / order-management domain logic.

This module is the single source of truth for business rules. It is exposed to the agents
through the MCP server (threadroute.mcp_server) - agents never touch the database directly.
Read functions are safe for agents; write functions are only called by the human-approval
executor (threadroute.approvals.executor).
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

from threadroute import db
from threadroute.config import get_settings
from threadroute.data.seed import haversine_km, seed_database
from threadroute.domain import constants as C


class WmsError(Exception):
    """A business-rule or lookup failure. Carries a stable machine code and a human message."""

    def __init__(self, code: str, message: str, **details: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self) -> dict:
        return {"ok": False, "error": {"code": self.code, "message": self.message, **self.details}}


def ensure_db() -> None:
    path = get_settings().wms_db
    if not path.exists():
        seed_database(path, reset=False)


def _session():
    ensure_db()
    return db.session(get_settings().wms_db)


def _event(conn, entity: str, entity_id: str, type_: str, payload: dict) -> None:
    conn.execute(
        "INSERT INTO events (ts, entity, entity_id, type, payload_json) VALUES (?,?,?,?,?)",
        (db.now_iso(), entity, entity_id, type_, db.dumps(payload)),
    )


def _one(conn, sql: str, params: tuple = ()):
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def _location(conn, location_id: str) -> dict:
    loc = _one(conn, "SELECT * FROM locations WHERE location_id=?", (location_id,))
    if not loc:
        raise WmsError("LOCATION_NOT_FOUND", f"Unknown location '{location_id}'.")
    return loc


def _cover(qty: int, velocity: float | None) -> float | None:
    if not velocity or velocity <= 0:
        return None
    return round(qty / velocity, 1)


# --------------------------------------------------------------------------- reads
def list_locations() -> list[dict]:
    with _session() as conn:
        return db.rows(conn.execute("SELECT * FROM locations ORDER BY kind, location_id"))


def get_sku(sku: str) -> dict:
    with _session() as conn:
        row = _one(conn, "SELECT * FROM skus WHERE sku=?", (sku,))
        if not row:
            raise WmsError("SKU_NOT_FOUND", f"Unknown SKU '{sku}'.")
        row["sizes"] = C.SIZE_SETS[row["category"]]
        return row


def _shipment_view(conn, row: dict) -> dict:
    items = db.loads(row.pop("items_json"), [])
    for it in items:
        sku = _one(conn, "SELECT name, unit_cost FROM skus WHERE sku=?", (it["sku"],))
        it["name"] = sku["name"] if sku else None
        it["unit_cost"] = sku["unit_cost"] if sku else None
    promised, eta = date.fromisoformat(row["promised_date"]), date.fromisoformat(row["eta"])
    row["items"] = items
    row["total_units"] = sum(i["qty"] for i in items)
    row["value_at_risk"] = round(sum((i["unit_cost"] or 0) * i["qty"] for i in items), 2)
    row["days_late"] = max(0, (eta - promised).days)
    row["origin"] = _location(conn, row["origin_id"])["name"]
    row["destination"] = _location(conn, row["destination_id"])["name"]
    return row


def list_shipments(status: str | None = None, destination_id: str | None = None,
                   carrier: str | None = None, delayed_only: bool = False, limit: int = 20) -> list[dict]:
    sql, params = "SELECT * FROM shipments WHERE 1=1", []
    if status:
        sql += " AND status=?"; params.append(status)
    if destination_id:
        sql += " AND destination_id=?"; params.append(destination_id)
    if carrier:
        sql += " AND carrier=?"; params.append(carrier)
    if delayed_only:
        sql += " AND status IN ('delayed','held')"
    sql += " ORDER BY promised_date, shipment_id LIMIT ?"
    params.append(max(1, min(int(limit), 50)))
    with _session() as conn:
        return [_shipment_view(conn, dict(r)) for r in conn.execute(sql, params).fetchall()]


def get_shipment(shipment_id: str) -> dict:
    with _session() as conn:
        row = _one(conn, "SELECT * FROM shipments WHERE shipment_id=?", (shipment_id.upper(),))
        if not row:
            raise WmsError("SHIPMENT_NOT_FOUND", f"No shipment '{shipment_id}'.")
        return _shipment_view(conn, row)


def get_purchase_order(po_id: str) -> dict:
    with _session() as conn:
        row = _one(conn, "SELECT * FROM purchase_orders WHERE po_id=?", (po_id.upper(),))
        if not row:
            raise WmsError("PO_NOT_FOUND", f"No purchase order '{po_id}'.")
        row["shipments"] = [r["shipment_id"] for r in conn.execute(
            "SELECT shipment_id FROM shipments WHERE po_id=?", (row["po_id"],))]
        return row


def get_inventory(location_id: str | None = None, sku: str | None = None, size: str | None = None,
                  low_stock_only: bool = False, limit: int = 25) -> list[dict]:
    sql = ("SELECT i.location_id, l.name AS location_name, l.kind, i.sku, s.name AS sku_name, i.size, "
           "i.qty, i.weekly_velocity FROM inventory i JOIN locations l USING(location_id) "
           "JOIN skus s USING(sku) WHERE 1=1")
    params: list = []
    for col, val in (("i.location_id", location_id), ("i.sku", sku), ("i.size", size)):
        if val:
            sql += f" AND {col}=?"; params.append(val)
    if low_stock_only:
        sql += " AND i.weekly_velocity > 0 AND (i.qty * 1.0 / i.weekly_velocity) < 1.5"
    sql += " ORDER BY (i.qty * 1.0 / MAX(i.weekly_velocity, 0.1)), i.location_id LIMIT ?"
    params.append(max(1, min(int(limit), 50)))
    with _session() as conn:
        out = db.rows(conn.execute(sql, params))
    for r in out:
        r["weeks_of_cover"] = _cover(r["qty"], r["weekly_velocity"])
        r["stockout_in_days"] = round(r["qty"] / r["weekly_velocity"] * 7, 1) if r["weekly_velocity"] else None
    return out


def get_stock_cover(sku: str, size: str) -> dict:
    rows = get_inventory(sku=sku, size=size, limit=50)
    if not rows:
        raise WmsError("INVENTORY_NOT_FOUND", f"No inventory rows for {sku} size {size}.")
    return {
        "sku": sku, "size": size,
        "network_qty": sum(r["qty"] for r in rows),
        "network_weekly_velocity": round(sum(r["weekly_velocity"] for r in rows), 1),
        "locations": rows,
    }


def _surplus(qty: int, velocity: float, kind: str) -> int:
    keep = math.ceil(velocity * C.SAFETY_WEEKS[kind])
    return max(0, qty - keep)


def transferable_surplus(location_id: str, sku: str, size: str) -> int:
    """How many units a location can release while keeping its safety cover (read-only)."""
    with _session() as conn:
        loc = _location(conn, location_id)
        row = _one(conn, "SELECT * FROM inventory WHERE location_id=? AND sku=? AND size=?", (location_id, sku, size))
        if not row:
            raise WmsError("INVENTORY_NOT_FOUND", f"{sku} size {size} is not stocked at {location_id}.")
        return _surplus(row["qty"], row["weekly_velocity"], loc["kind"])


def find_donor_stores(sku: str, size: str, to_location_id: str, needed_qty: int, limit: int = 5) -> dict:
    """Locations that can give stock away while keeping their own safety cover, nearest first."""
    with _session() as conn:
        dest = _location(conn, to_location_id)
        rows = db.rows(conn.execute(
            "SELECT i.*, l.kind, l.name, l.city, l.lat, l.lon FROM inventory i JOIN locations l USING(location_id) "
            "WHERE i.sku=? AND i.size=? AND i.location_id<>?", (sku, size, to_location_id)))
    donors = []
    for r in rows:
        surplus = _surplus(r["qty"], r["weekly_velocity"], r["kind"])
        if surplus <= 0:
            continue
        dist = haversine_km(dest["lat"], dest["lon"], r["lat"], r["lon"])
        donors.append({
            "location_id": r["location_id"], "name": r["name"], "kind": r["kind"], "qty": r["qty"],
            "weekly_velocity": r["weekly_velocity"], "transferable_surplus": surplus,
            "distance_km": round(dist), "can_cover_full_need": surplus >= needed_qty,
        })
    donors.sort(key=lambda d: (not d["can_cover_full_need"], d["distance_km"]))
    return {"sku": sku, "size": size, "to_location_id": to_location_id, "needed_qty": needed_qty,
            "safety_policy_weeks": C.SAFETY_WEEKS, "donors": donors[: max(1, min(limit, 10))]}


def list_returns(status: str | None = None, limit: int = 20) -> list[dict]:
    sql, params = "SELECT r.*, s.name AS sku_name FROM returns r JOIN skus s USING(sku)", []
    if status:
        sql += " WHERE r.status=?"; params.append(status)
    sql += " ORDER BY return_id LIMIT ?"; params.append(max(1, min(int(limit), 50)))
    with _session() as conn:
        return db.rows(conn.execute(sql, params))


def get_return(return_id: str) -> dict:
    with _session() as conn:
        row = _one(conn, "SELECT r.*, s.name AS sku_name, s.price, s.unit_cost, s.category FROM returns r "
                         "JOIN skus s USING(sku) WHERE return_id=?", (return_id.upper(),))
        if not row:
            raise WmsError("RETURN_NOT_FOUND", f"No return '{return_id}'.")
        return row


def list_claims(shipment_id: str | None = None) -> list[dict]:
    with _session() as conn:
        if shipment_id:
            cur = conn.execute("SELECT * FROM claims WHERE shipment_id=? ORDER BY created_at", (shipment_id.upper(),))
        else:
            cur = conn.execute("SELECT * FROM claims ORDER BY created_at DESC LIMIT 25")
        return db.rows(cur)


# --------------------------------------------------------------------------- writes
def reallocate_stock(from_location: str, to_location: str, sku: str, size: str, qty: int,
                     reason: str = "") -> dict:
    qty = int(qty)
    if qty <= 0:
        raise WmsError("INVALID_QTY", "Quantity must be a positive number.")
    if from_location == to_location:
        raise WmsError("SAME_LOCATION", "Source and destination must differ.")
    with _session() as conn:
        src = _location(conn, from_location)
        _location(conn, to_location)
        row = _one(conn, "SELECT * FROM inventory WHERE location_id=? AND sku=? AND size=?",
                   (from_location, sku, size))
        dst = _one(conn, "SELECT * FROM inventory WHERE location_id=? AND sku=? AND size=?",
                   (to_location, sku, size))
        if not row or not dst:
            raise WmsError("INVENTORY_NOT_FOUND", f"{sku} size {size} is not stocked at both locations.")
        surplus = _surplus(row["qty"], row["weekly_velocity"], src["kind"])
        if qty > surplus:
            raise WmsError("INSUFFICIENT_SURPLUS",
                           f"{from_location} can release at most {surplus} units of {sku}/{size} "
                           f"(keeps {C.SAFETY_WEEKS[src['kind']]} weeks of cover).",
                           transferable_surplus=surplus)
        conn.execute("UPDATE inventory SET qty=qty-? WHERE location_id=? AND sku=? AND size=?",
                     (qty, from_location, sku, size))
        conn.execute("UPDATE inventory SET qty=qty+? WHERE location_id=? AND sku=? AND size=?",
                     (qty, to_location, sku, size))
        n = conn.execute("SELECT COUNT(*) FROM transfers").fetchone()[0]
        tid = f"TRF-{3001 + n}"
        conn.execute("INSERT INTO transfers VALUES (?,?,?,?,?,?,?,?,?)",
                     (tid, from_location, to_location, sku, size, qty, "in_transit", reason, db.now_iso()))
        _event(conn, "transfer", tid, "stock_reallocated",
               {"from": from_location, "to": to_location, "sku": sku, "size": size, "qty": qty})
    return {"ok": True, "transfer_id": tid, "from": from_location, "to": to_location,
            "sku": sku, "size": size, "qty": qty}


def create_claim(shipment_id: str, claim_type: str, damage_type: str, severity: str,
                 affected_units: int, details: dict | None = None) -> dict:
    if claim_type not in C.CLAIM_TYPES:
        raise WmsError("INVALID_CLAIM_TYPE", f"claim_type must be one of {sorted(C.CLAIM_TYPES)}.")
    if damage_type not in C.DAMAGE_TYPES:
        raise WmsError("INVALID_DAMAGE_TYPE", f"damage_type must be one of {sorted(C.DAMAGE_TYPES)}.")
    if severity not in C.SEVERITIES:
        raise WmsError("INVALID_SEVERITY", f"severity must be one of {sorted(C.SEVERITIES)}.")
    shipment = get_shipment(shipment_id)
    affected_units = int(affected_units)
    if not 0 < affected_units <= shipment["total_units"]:
        raise WmsError("INVALID_UNITS",
                       f"affected_units must be between 1 and {shipment['total_units']} for {shipment['shipment_id']}.")
    total = shipment["total_units"]
    avg_cost = sum((i["unit_cost"] or 0) * i["qty"] for i in shipment["items"]) / total
    value = round(avg_cost * affected_units, 2)
    with _session() as conn:
        n = conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
        cid = f"CL-{2001 + n}"
        conn.execute("INSERT INTO claims VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (cid, shipment["shipment_id"], claim_type, damage_type, severity, affected_units,
                      value, "filed", db.dumps(details or {}), db.now_iso()))
        _event(conn, "claim", cid, "claim_filed", {"shipment": shipment["shipment_id"], "value": value})
    return {"ok": True, "claim_id": cid, "shipment_id": shipment["shipment_id"], "claim_value": value,
            "currency": C.CURRENCY, "carrier": shipment["carrier"]}


_SPEED_KMH = {"road": 55.0, "rail": 40.0, "air": 600.0}


def reroute_shipment(shipment_id: str, new_mode: str, new_carrier: str, reason: str = "",
                     new_eta: str | None = None) -> dict:
    shipment = get_shipment(shipment_id)
    if shipment["status"] in ("delivered", "cancelled"):
        raise WmsError("NOT_REROUTABLE", f"{shipment['shipment_id']} is already {shipment['status']}.")
    if new_carrier not in C.CARRIERS:
        raise WmsError("UNKNOWN_CARRIER", f"Unknown carrier '{new_carrier}'. Options: {sorted(C.CARRIERS)}.")
    if new_mode not in C.CARRIERS[new_carrier]["modes"]:
        raise WmsError("MODE_NOT_SUPPORTED",
                       f"{new_carrier} does not offer {new_mode}. It offers {C.CARRIERS[new_carrier]['modes']}.")
    with _session() as conn:
        origin = _location(conn, shipment["origin_id"])
        dest = _location(conn, shipment["destination_id"])
        if new_eta is None:
            lat = shipment["last_lat"] if shipment.get("last_lat") else origin["lat"]
            lon = shipment["last_lon"] if shipment.get("last_lon") else origin["lon"]
            km = haversine_km(lat, lon, dest["lat"], dest["lon"]) * (1.25 if new_mode != "air" else 1.0)
            hours = km / _SPEED_KMH[new_mode] + (8 if new_mode == "air" else 12)
            new_eta = (date.today() + timedelta(days=math.ceil(hours / 24))).isoformat()
        conn.execute("UPDATE shipments SET carrier=?, mode=?, status='in_transit', eta=?, hold_reason=NULL, "
                     "last_event=?, updated_at=? WHERE shipment_id=?",
                     (new_carrier, new_mode, new_eta, f"Rerouted via {new_carrier} ({new_mode})",
                      db.now_iso(), shipment["shipment_id"]))
        _event(conn, "shipment", shipment["shipment_id"], "shipment_rerouted",
               {"carrier": new_carrier, "mode": new_mode, "eta": new_eta, "reason": reason})
    return {"ok": True, "shipment_id": shipment["shipment_id"], "carrier": new_carrier,
            "mode": new_mode, "new_eta": new_eta}


def set_return_disposition(return_id: str, grade: str, disposition: str, notes: str = "") -> dict:
    if grade not in C.RETURN_GRADES:
        raise WmsError("INVALID_GRADE", f"grade must be one of {sorted(C.RETURN_GRADES)}.")
    if disposition not in C.RETURN_DISPOSITIONS:
        raise WmsError("INVALID_DISPOSITION", f"disposition must be one of {sorted(C.RETURN_DISPOSITIONS)}.")
    if disposition not in C.GRADE_DISPOSITIONS[grade]:
        raise WmsError("GRADE_DISPOSITION_MISMATCH",
                       f"Grade {grade} allows {sorted(C.GRADE_DISPOSITIONS[grade])}, not '{disposition}'.")
    ret = get_return(return_id)
    with _session() as conn:
        conn.execute("UPDATE returns SET grade=?, disposition=?, notes=?, status='graded' WHERE return_id=?",
                     (grade, disposition, notes, ret["return_id"]))
        if disposition == "restock":
            conn.execute("UPDATE inventory SET qty=qty+1 WHERE location_id=? AND sku=? AND size=?",
                         (ret["location_id"], ret["sku"], ret["size"]))
        _event(conn, "return", ret["return_id"], "return_graded", {"grade": grade, "disposition": disposition})
    return {"ok": True, "return_id": ret["return_id"], "grade": grade, "disposition": disposition}
