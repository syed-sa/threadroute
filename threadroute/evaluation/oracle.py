"""Ground truth for the evaluators: the WMS database itself (we own the data, so truth is checkable)."""

from __future__ import annotations

import re
import sqlite3

from threadroute.config import get_settings
from threadroute.domain import wms

STATIC_KNOWN = {"DC-BHW", "DC-GGN"}  # named in the system prompt, so legitimately known without a tool call

ID_PATTERNS = {
    "shipment": re.compile(r"\bSH-\d{4}\b"),
    "po": re.compile(r"\bPO-\d{4}\b"),
    "return": re.compile(r"\bRT-\d{4}\b"),
    "claim": re.compile(r"\bCL-\d{4}\b"),
    "order": re.compile(r"\bORD-\d{5}\b"),
    "action": re.compile(r"\bACT-\d{4}\b"),
    "transfer": re.compile(r"\bTRF-\d{3,6}\b"),
    "sku": re.compile(r"\b[A-Z]{2}-[A-Z]{3}-[A-Z]{3}\b"),
    "location": re.compile(r"\b(?:DC|ST)-[A-Z]{3}(?:-[A-Z]{2,3})?\b"),
}


class Catalog:
    """Sets of ids that really exist in the WMS."""

    def __init__(self) -> None:
        wms.ensure_db()
        conn = sqlite3.connect(str(get_settings().wms_db))
        try:
            q = lambda sql: {r[0] for r in conn.execute(sql).fetchall()}  # noqa: E731
            self.ids = {
                "shipment": q("SELECT shipment_id FROM shipments"), "po": q("SELECT po_id FROM purchase_orders"),
                "return": q("SELECT return_id FROM returns"), "claim": q("SELECT claim_id FROM claims"),
                "order": q("SELECT order_id FROM returns"), "sku": q("SELECT sku FROM skus"),
                "location": q("SELECT location_id FROM locations"), "action": set(),
                "transfer": q("SELECT transfer_id FROM transfers"),
            }
            self.carriers = q("SELECT DISTINCT carrier FROM shipments")
        finally:
            conn.close()

    def exists(self, kind: str, value: str) -> bool:
        if kind == "location" and value in STATIC_KNOWN:
            return True
        return value in self.ids.get(kind, set())


def shipment_facts(shipment_id: str) -> dict | None:
    try:
        return wms.get_shipment(shipment_id)
    except wms.WmsError:
        return None
