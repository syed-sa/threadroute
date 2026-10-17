"""MCP server exposing the warehouse/order-management system to agents.

Two modes (env THREADROUTE_MCP_MODE):
  read  - only read tools are registered. This is what the LLM agents connect to.
  full  - read + write tools. Only the human-approval executor connects in this mode, so an
          agent can never mutate stock, claims or shipments without a person approving it.
"""

from __future__ import annotations

import functools
import os
from typing import Any, Callable

from mcp.server.fastmcp import FastMCP

from threadroute.domain import wms
from threadroute.domain.wms import WmsError

READ_TOOLS = [
    "list_locations", "get_sku", "list_shipments", "get_shipment", "get_purchase_order",
    "get_inventory", "get_stock_cover", "find_donor_stores", "list_returns", "get_return", "list_claims",
]
WRITE_TOOLS = ["reallocate_stock", "create_claim", "reroute_shipment", "set_return_disposition"]


def _safe(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Turn business-rule errors into structured results the agent (or executor) can act on."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except WmsError as exc:
            return exc.to_dict()

    return wrapper


def build_server(mode: str | None = None) -> FastMCP:
    mode = (mode or os.environ.get("THREADROUTE_MCP_MODE", "read")).lower()
    mcp = FastMCP("threadroute-wms", log_level="WARNING")

    # ---------------- read tools ----------------
    @mcp.tool()
    @_safe
    def list_locations() -> list[dict]:
        """List all distribution centres (kind=dc) and stores (kind=store) with city and region."""
        return wms.list_locations()

    @mcp.tool()
    @_safe
    def get_sku(sku: str) -> dict:
        """Get a SKU's name, category, handling type (hanging/folded), cost, price and size run."""
        return wms.get_sku(sku)

    @mcp.tool()
    @_safe
    def list_shipments(status: str = "", destination_id: str = "", carrier: str = "",
                       delayed_only: bool = False, limit: int = 20) -> list[dict]:
        """List shipments. Filter by status (in_transit, delayed, held, delivered, pending_pickup),
        destination location id (e.g. ST-DEL-CP or DC-GGN), carrier, or delayed_only=true."""
        return wms.list_shipments(status or None, destination_id or None, carrier or None, delayed_only, limit)

    @mcp.tool()
    @_safe
    def get_shipment(shipment_id: str) -> dict:
        """Get one shipment (e.g. SH-1001): items with SKU names, carrier, mode, status, promised date,
        ETA, days_late, hold reason, last known location and value at risk (INR)."""
        return wms.get_shipment(shipment_id)

    @mcp.tool()
    @_safe
    def get_purchase_order(po_id: str) -> dict:
        """Get a purchase order (e.g. PO-5001): supplier, SKU, ordered vs received quantity and linked shipments."""
        return wms.get_purchase_order(po_id)

    @mcp.tool()
    @_safe
    def get_inventory(location_id: str = "", sku: str = "", size: str = "",
                      low_stock_only: bool = False, limit: int = 25) -> list[dict]:
        """Inventory rows with weekly sales velocity, weeks_of_cover and stockout_in_days.
        low_stock_only=true returns rows with under 1.5 weeks of cover. Leave a filter empty to ignore it."""
        return wms.get_inventory(location_id or None, sku or None, size or None, low_stock_only, limit)

    @mcp.tool()
    @_safe
    def get_stock_cover(sku: str, size: str) -> dict:
        """Network-wide stock for one SKU and size: quantity and velocity at every location."""
        return wms.get_stock_cover(sku, size)

    @mcp.tool()
    @_safe
    def find_donor_stores(sku: str, size: str, to_location_id: str, needed_qty: int, limit: int = 5) -> dict:
        """Find locations that can give away stock of a SKU/size while keeping their own safety cover
        (3 weeks for stores, 2 for DCs). Sorted: full-cover donors first, then nearest."""
        return wms.find_donor_stores(sku, size, to_location_id, needed_qty, limit)

    @mcp.tool()
    @_safe
    def list_returns(status: str = "", limit: int = 20) -> list[dict]:
        """List customer returns (status received_at_dc, graded, closed)."""
        return wms.list_returns(status or None, limit)

    @mcp.tool()
    @_safe
    def get_return(return_id: str) -> dict:
        """Get one return (e.g. RT-9001): SKU, size, customer reason, price, cost, current grade/disposition."""
        return wms.get_return(return_id)

    @mcp.tool()
    @_safe
    def list_claims(shipment_id: str = "") -> list[dict]:
        """List carrier claims, optionally for one shipment."""
        return wms.list_claims(shipment_id or None)

    # ---------------- write tools (executor only) ----------------
    if mode == "full":

        @mcp.tool()
        @_safe
        def reallocate_stock(from_location: str, to_location: str, sku: str, size: str, qty: int,
                             reason: str = "") -> dict:
            """Move stock between locations. Enforces donor safety-cover rules."""
            return wms.reallocate_stock(from_location, to_location, sku, size, qty, reason)

        @mcp.tool()
        @_safe
        def create_claim(shipment_id: str, claim_type: str, damage_type: str, severity: str,
                         affected_units: int, details: dict | None = None) -> dict:
            """File a carrier claim (damage, shortage or loss) against a shipment."""
            return wms.create_claim(shipment_id, claim_type, damage_type, severity, affected_units, details)

        @mcp.tool()
        @_safe
        def reroute_shipment(shipment_id: str, new_mode: str, new_carrier: str, reason: str = "",
                             new_eta: str | None = None) -> dict:
            """Move an undelivered shipment to a different carrier/mode."""
            return wms.reroute_shipment(shipment_id, new_mode, new_carrier, reason, new_eta)

        @mcp.tool()
        @_safe
        def set_return_disposition(return_id: str, grade: str, disposition: str, notes: str = "") -> dict:
            """Record the grade (A-D) and disposition (restock/refurbish/liquidate/recycle) of a return."""
            return wms.set_return_disposition(return_id, grade, disposition, notes)

    return mcp
