"""External API integrations. Each function goes through resilient_call (retry, breaker, fallback)."""

from threadroute.integrations.carriers import track_shipment
from threadroute.integrations.gmail import send_email
from threadroute.integrations.maps import estimate_route
from threadroute.integrations.sheets import log_action
from threadroute.integrations.weather import get_weather_risk

__all__ = ["track_shipment", "estimate_route", "get_weather_risk", "send_email", "log_action"]
