"""A small circuit breaker: closed -> open (after N failures) -> half-open (after cool-down)."""

from __future__ import annotations

import threading
import time

_clock = time.monotonic  # tests may monkeypatch this


class CircuitBreaker:
    def __init__(self, name: str, failure_threshold: int = 3, recovery_seconds: float = 30.0):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds
        self._failures = 0
        self._opened_at: float | None = None
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            if self._opened_at is None:
                return "closed"
            if _clock() - self._opened_at >= self.recovery_seconds:
                return "half_open"
            return "open"

    def allow(self) -> bool:
        return self.state in ("closed", "half_open")

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None

    def record_failure(self) -> bool:
        """Returns True if this failure opened (or re-opened) the breaker."""
        with self._lock:
            self._failures += 1
            half_open = self._opened_at is not None and _clock() - self._opened_at >= self.recovery_seconds
            if half_open or self._failures >= self.failure_threshold:
                self._opened_at = _clock()
                return True
            return False

    def reset(self) -> None:
        self.record_success()


_registry: dict[str, CircuitBreaker] = {}
_registry_lock = threading.Lock()


def get_breaker(name: str) -> CircuitBreaker:
    with _registry_lock:
        if name not in _registry:
            _registry[name] = CircuitBreaker(name)
        return _registry[name]


def all_states() -> dict[str, str]:
    with _registry_lock:
        return {name: b.state for name, b in sorted(_registry.items())}


def reset_all() -> None:
    with _registry_lock:
        _registry.clear()
