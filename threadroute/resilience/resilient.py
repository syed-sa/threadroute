"""resilient_call: run a function through an ordered chain of providers with retry, timeout,
circuit breakers, schema validation and automatic fallback. Every healing action is logged."""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, ValidationError

from threadroute.resilience import chaos, store
from threadroute.resilience.breaker import get_breaker


@dataclass
class Provider:
    name: str
    call: Callable[..., Awaitable[dict]]
    degraded: bool = False  # True for estimates / caches / local fallbacks


class AllProvidersFailed(RuntimeError):
    def __init__(self, component: str, errors: list[str]):
        super().__init__(f"All providers failed for '{component}': " + "; ".join(errors))
        self.component = component
        self.errors = errors


def effective_order(component: str, providers: list[Provider]) -> list[Provider]:
    cfg = store.get_config(component)
    active = [p for p in providers if p.name not in cfg.disabled]
    demoted = set(cfg.active_demotions())
    return [p for p in active if p.name not in demoted] + [p for p in active if p.name in demoted]


async def resilient_call(component: str, providers: list[Provider], *args: Any,
                         schema: type[BaseModel] | None = None, **kwargs: Any) -> dict:
    cfg = store.get_config(component)
    order = effective_order(component, providers)
    if not order:
        raise AllProvidersFailed(component, ["no providers enabled"])
    errors: list[str] = []
    for idx, prov in enumerate(order):
        target = f"{component}:{prov.name}"
        breaker = get_breaker(target)
        if not breaker.allow():
            errors.append(f"{prov.name}: circuit open")
            store.log_event(component, prov.name, "breaker_skip", "circuit open - skipping provider")
            continue
        for attempt in range(cfg.max_retries + 1):
            started = time.perf_counter()
            try:
                mutation = await chaos.apply(target)
                result = await asyncio.wait_for(prov.call(*args, **kwargs), timeout=cfg.timeout)
                if mutation == "malformed":
                    result = chaos.corrupt(result)
                if schema is not None:
                    result = schema.model_validate(result).model_dump()
                latency = (time.perf_counter() - started) * 1000
                breaker.record_success()
                store.record_call(target, True, latency)
                if idx > 0 or attempt > 0:
                    store.log_event(component, prov.name, "recovered",
                                    f"succeeded via '{prov.name}' on attempt {attempt + 1} "
                                    f"after {len(errors)} earlier failure(s)")
                result = dict(result)
                result["source"] = prov.name
                result["degraded"] = prov.degraded
                if idx > 0 or attempt > 0 or prov.degraded:
                    result["healing_note"] = (f"Preferred source unavailable; served by '{prov.name}'"
                                              + (" (fallback estimate - lower confidence)" if prov.degraded else ""))
                if idx > 0:
                    from threadroute.resilience import watchdog
                    watchdog.auto_heal(component)
                return result
            except (asyncio.TimeoutError, TimeoutError) as exc:
                err = f"timeout after {cfg.timeout}s" if not str(exc) else str(exc)
                kind = "timeout"
            except ValidationError as exc:
                err = f"schema validation failed ({exc.error_count()} issue(s))"
                kind = "schema"
            except Exception as exc:  # noqa: BLE001 - any provider failure must be healed, not crash
                err = f"{type(exc).__name__}: {exc}"
                kind = "failure"
            latency = (time.perf_counter() - started) * 1000
            store.record_call(target, False, latency, err)
            store.log_event(component, prov.name, kind, f"attempt {attempt + 1}: {err}")
            errors.append(f"{prov.name}: {err}")
            if attempt < cfg.max_retries:
                delay = cfg.base_delay * (2 ** attempt) * (0.5 + random.random())
                store.log_event(component, prov.name, "retry", f"retrying in {delay:.2f}s")
                await asyncio.sleep(delay)
        if breaker.record_failure():
            store.log_event(component, prov.name, "breaker_open", "circuit opened after repeated failures")
        store.log_event(component, prov.name, "fallback", "provider exhausted - trying next source")
    store.log_event(component, None, "exhausted", "; ".join(errors)[:400])
    raise AllProvidersFailed(component, errors)


def unavailable_as_error(fn):
    """Decorator for agent-facing integrations: a total outage becomes a structured result the model can
    explain to the user ("the carrier feed is down") instead of an exception."""
    import functools

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except AllProvidersFailed as exc:
            return {"ok": False, "error": {"code": "SERVICE_UNAVAILABLE", "component": exc.component,
                                           "message": f"All data sources for '{exc.component}' are unavailable right now.",
                                           "details": exc.errors[:4]}}

    return wrapper
