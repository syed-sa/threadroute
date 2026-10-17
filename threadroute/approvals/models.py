"""Validated payloads for every action an agent may propose. Pydantic errors are returned to the
agent as structured feedback so it can correct its own arguments (schema self-repair)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReallocateStockPayload(_Payload):
    from_location: str
    to_location: str
    sku: str
    size: str
    qty: int = Field(gt=0, le=5000)
    reason: str = ""


class FileClaimPayload(_Payload):
    shipment_id: str
    claim_type: Literal["damage", "shortage", "loss"]
    damage_type: Literal["crushed_carton", "torn_carton", "water_damage", "stained_garments",
                         "creased_hanging", "seal_broken", "other", "none"]
    severity: Literal["minor", "moderate", "severe"]
    affected_units: int = Field(gt=0, le=5000)
    evidence_summary: str = Field(min_length=10)


class ReroutePayload(_Payload):
    shipment_id: str
    new_mode: Literal["road", "rail", "air"]
    new_carrier: Literal["SwiftShip", "RoadRunner Express", "AirLink Cargo"]
    reason: str = Field(min_length=5)
    new_eta: str | None = None


class ReturnDispositionPayload(_Payload):
    return_id: str
    grade: Literal["A", "B", "C", "D"]
    disposition: Literal["restock", "refurbish", "liquidate", "recycle"]
    notes: str = ""


class SendEmailPayload(_Payload):
    to: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    subject: str = Field(min_length=3)
    body: str = Field(min_length=10)


PAYLOAD_MODELS: dict[str, type[BaseModel]] = {
    "reallocate_stock": ReallocateStockPayload,
    "file_claim": FileClaimPayload,
    "reroute_shipment": ReroutePayload,
    "set_return_disposition": ReturnDispositionPayload,
    "send_email": SendEmailPayload,
}
