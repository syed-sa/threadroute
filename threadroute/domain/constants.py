"""Shared constants and enums-as-sets for the ThreadRoute domain."""

from __future__ import annotations

CURRENCY = "INR"

SIZE_SETS = {
    "shirt": ["S", "M", "L", "XL", "XXL"],
    "polo": ["S", "M", "L", "XL", "XXL"],
    "jacket": ["S", "M", "L", "XL", "XXL"],
    "trouser": ["30", "32", "34", "36", "38"],
    "denim": ["30", "32", "34", "36", "38"],
    "blazer": ["38", "40", "42", "44", "46"],
    "suit": ["38", "40", "42", "44", "46"],
}

# Approximate packed weight per unit (kg) - used for freight cost estimates.
UNIT_WEIGHT_KG = {
    "shirt": 0.3, "polo": 0.25, "trouser": 0.5, "denim": 0.7,
    "blazer": 1.1, "suit": 1.6, "jacket": 0.9,
}

# Hanging garments need garment-on-hanger (GOH) handling in transit.
HANDLING_BY_CATEGORY = {
    "shirt": "folded", "polo": "folded", "trouser": "folded", "denim": "folded",
    "blazer": "hanging", "suit": "hanging", "jacket": "folded",
}

SHIPMENT_STATUSES = {"pending_pickup", "in_transit", "delayed", "delivered", "held", "cancelled"}
TRANSPORT_MODES = {"road", "rail", "air"}

# carrier -> supported modes and relative cost per kg-km (INR)
CARRIERS = {
    "SwiftShip": {"modes": ["road", "rail"], "rate_per_kg_km": {"road": 0.012, "rail": 0.008}},
    "RoadRunner Express": {"modes": ["road"], "rate_per_kg_km": {"road": 0.014}},
    "AirLink Cargo": {"modes": ["air"], "rate_per_kg_km": {"air": 0.06}},
}

CLAIM_TYPES = {"damage", "shortage", "loss"}
DAMAGE_TYPES = {
    "crushed_carton", "torn_carton", "water_damage", "stained_garments",
    "creased_hanging", "seal_broken", "other", "none",
}
SEVERITIES = {"minor", "moderate", "severe"}

RETURN_DISPOSITIONS = {"restock", "refurbish", "liquidate", "recycle"}
RETURN_GRADES = {"A", "B", "C", "D"}
RETURN_STATUSES = {"received_at_dc", "graded", "closed"}
# Grade -> allowed dispositions (A is resellable as new, D is unsalvageable)
GRADE_DISPOSITIONS = {
    "A": {"restock"},
    "B": {"refurbish", "restock"},
    "C": {"liquidate", "refurbish"},
    "D": {"recycle", "liquidate"},
}

# Network policy: weeks of sales cover a location must keep after giving stock away.
SAFETY_WEEKS = {"store": 3, "dc": 2}

ACTION_KINDS = {
    "reallocate_stock", "file_claim", "reroute_shipment",
    "set_return_disposition", "send_email",
}
