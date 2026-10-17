"""Tiny in-memory TTL cache for read-only integration lookups (carrier status, weather, route estimates).

Why: one chat turn often asks for the same weather/route/tracking several times (reroute comparison, repeated
questions). A hit costs microseconds and saves a network round trip. Safety rules:
  * only good answers are cached - never an error and never a degraded/fallback answer;
  * entries expire (THREADROUTE_CACHE_TTL seconds, default 20; 0 disables the cache);
  * the whole cache is dropped whenever state changes (chaos rule set/cleared, an action executed, healing applied),
    so a demo never shows stale data;
  * bounded size, no disk, no logging."""

from __future__ import annotations

import functools
import os
import threading
import time
from collections import OrderedDict

from threadroute.perf.metrics import metrics

MAX_ENTRIES = 512
_lock = threading.Lock()
_store: "OrderedDict[tuple, tuple[float, object]]" = OrderedDict()


def ttl() -> float:
    try:
        return max(0.0, float(os.environ.get("THREADROUTE_CACHE_TTL", "20")))
    except ValueError:
        return 20.0


def invalidate() -> None:
    with _lock:
        _store.clear()


def _cacheable(result) -> bool:
    return isinstance(result, dict) and result.get("ok") is not False and not result.get("degraded")


def cached(name: str):
    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            limit = ttl()
            if limit <= 0:
                return await fn(*args, **kwargs)
            key = (name, args, tuple(sorted(kwargs.items())))
            now = time.monotonic()
            with _lock:
                hit = _store.get(key)
                if hit and now - hit[0] < limit:
                    _store.move_to_end(key)
                    metrics.incr(f"cache_hit.{name}")
                    return dict(hit[1])
            result = await fn(*args, **kwargs)
            if _cacheable(result):
                with _lock:
                    _store[key] = (now, dict(result))
                    while len(_store) > MAX_ENTRIES:
                        _store.popitem(last=False)
            metrics.incr(f"cache_miss.{name}")
            return result

        return wrapper

    return deco
