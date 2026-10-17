"""Guardrail audit trail. Events carry only the *kind* and *count* of what was caught - never the value."""

from __future__ import annotations

import threading
import time
from collections import Counter, deque

_lock = threading.Lock()
_recent: deque[dict] = deque(maxlen=500)
_totals: Counter = Counter()


def record(stage: str, kind: str, count: int, action: str, detail: str = "") -> None:
    if count <= 0:
        return
    with _lock:
        _totals[f"{stage}:{kind}:{action}"] += count
        _recent.append({"epoch": time.time(), "stage": stage, "kind": kind, "count": count, "action": action,
                        "detail": detail[:120]})


def summary() -> dict:
    with _lock:
        by_kind = Counter()
        by_stage = Counter()
        for key, n in _totals.items():
            stage, kind, _action = key.split(":", 2)
            by_kind[kind] += n
            by_stage[stage] += n
        return {"total": sum(_totals.values()), "by_kind": dict(by_kind), "by_stage": dict(by_stage),
                "recent": list(_recent)[-25:]}


def reset() -> None:
    with _lock:
        _recent.clear()
        _totals.clear()
