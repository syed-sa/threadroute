"""Per-session record of what the model was actually shown by tools (after redaction/compaction).
The grounding evaluator checks answers against this. In memory only, bounded."""

from __future__ import annotations

import threading
from collections import defaultdict, deque

_lock = threading.Lock()
_items: dict[str, deque] = defaultdict(lambda: deque(maxlen=80))
_counts: dict[str, int] = defaultdict(int)
_turn_start: dict[str, int] = {}


def add(session_id: str, tool: str, args: dict, result) -> None:
    with _lock:
        _items[session_id].append({"tool": tool, "args": args, "result": result, "n": _counts[session_id]})
        _counts[session_id] += 1


def mark_turn(session_id: str) -> None:
    with _lock:
        _turn_start[session_id] = _counts[session_id]


def since_turn(session_id: str) -> list[dict]:
    with _lock:
        start = _turn_start.get(session_id, 0)
        return [dict(i) for i in _items.get(session_id, []) if i["n"] >= start]


def all_for(session_id: str) -> list[dict]:
    with _lock:
        return [dict(i) for i in _items.get(session_id, [])]


def reset() -> None:
    with _lock:
        _items.clear()
        _counts.clear()
        _turn_start.clear()
