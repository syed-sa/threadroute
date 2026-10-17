"""Lightweight, in-memory timing spans and counters.

Design rules (they keep the app fast):
  * recording a span is a perf_counter() pair plus a deque append - microseconds, no I/O, no logging;
  * all state is bounded (fixed-size deques) so memory never grows;
  * nothing is persisted unless someone explicitly calls `snapshot()` (the report/benchmark/UI endpoints).
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from typing import Iterator

MAX_SAMPLES = 2000


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return ordered[k]


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._spans: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=MAX_SAMPLES))
        self._counters: dict[str, int] = defaultdict(int)
        self.enabled = True

    def record(self, name: str, ms: float) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._spans[name].append(ms)

    @contextmanager
    def span(self, name: str) -> Iterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            self.record(name, (time.perf_counter() - start) * 1000)

    def incr(self, name: str, n: int = 1) -> None:
        if self.enabled:
            with self._lock:
                self._counters[name] += n

    def counter(self, name: str) -> int:
        return self._counters.get(name, 0)

    def snapshot(self) -> dict:
        with self._lock:
            spans = {k: list(v) for k, v in self._spans.items()}
            counters = dict(self._counters)
        out = {}
        for name, vals in sorted(spans.items()):
            out[name] = {"count": len(vals), "p50_ms": round(_percentile(vals, 50), 2),
                         "p95_ms": round(_percentile(vals, 95), 2), "max_ms": round(max(vals), 2),
                         "total_ms": round(sum(vals), 1)}
        return {"spans": out, "counters": counters}

    def reset(self) -> None:
        with self._lock:
            self._spans.clear()
            self._counters.clear()


metrics = Metrics()
