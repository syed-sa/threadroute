"""External API integrations. Each function goes through resilient_call (retry, breaker, fallback);
the read-only lookups are also wrapped in a short TTL cache (see perf/cache.py)."""

from threadroute.integrations.carriers import track_shipment as _track_shipment
from threadroute.integrations.gmail import send_email
from threadroute.integrations.maps import estimate_route as _estimate_route
from threadroute.integrations.sheets import log_action
from threadroute.integrations.weather import get_weather_risk as _get_weather_risk
from threadroute.perf.cache import cached

track_shipment = cached("carrier")(_track_shipment)
estimate_route = cached("route")(_estimate_route)
get_weather_risk = cached("weather")(_get_weather_risk)

__all__ = ["track_shipment", "estimate_route", "get_weather_risk", "send_email", "log_action"]
