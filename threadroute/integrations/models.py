"""Pydantic response schemas. Every integration result is validated against one of these, so a
malformed upstream payload is detected and healed (retry / fallback) instead of reaching an agent."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore")


class TrackingEvent(_Base):
    ts: str | None = None
    location: str | None = None
    description: str


class CarrierTracking(_Base):
    shipment_id: str
    carrier: str
    status: str
    eta: str
    last_location: str | None = None
    last_event: str | None = None
    hold_reason: str | None = None
    events: list[TrackingEvent] = Field(default_factory=list)


class RouteEstimate(_Base):
    origin: str
    destination: str
    mode: Literal["road", "rail", "air"]
    distance_km: float = Field(gt=0)
    duration_hours: float = Field(gt=0)
    note: str | None = None


class WeatherDay(_Base):
    date: str
    precipitation_mm: float = 0
    wind_kmh: float = 0


class WeatherRisk(_Base):
    location: str
    risk_level: Literal["low", "moderate", "high", "severe", "unknown"]
    max_precipitation_mm: float
    max_wind_kmh: float
    summary: str
    days: list[WeatherDay] = Field(default_factory=list)


class EmailResult(_Base):
    message_id: str
    to: str
    subject: str
    status: Literal["sent", "simulated"]
    path: str | None = None


class SheetResult(_Base):
    tab: str
    status: Literal["appended", "logged_locally"]
    location: str
