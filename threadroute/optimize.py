"""Token and latency optimisations applied to what the model sees (switch off with THREADROUTE_OPTIMIZE=0
to measure the unoptimised baseline in `python -m threadroute benchmark`).

* MCP results arrive as pretty-printed JSON inside a text wrapper: parse, drop nulls, compact.
* Long lists / strings in tool results are truncated with an explicit "+N more" marker.
* Older turns: images from earlier messages and bulky old tool results are replaced by short stubs.
"""

from __future__ import annotations

import json
import os
from typing import Any

MAX_TOOL_CHARS = 3500
MAX_LIST_ITEMS = 12
MAX_STR = 300
KEEP_RECENT_CONTENTS = 6


def enabled() -> bool:
    return os.environ.get("THREADROUTE_OPTIMIZE", "1").strip().lower() not in {"0", "false", "off", "no"}


def _drop_nulls(o: Any) -> Any:
    if isinstance(o, dict):
        return {k: _drop_nulls(v) for k, v in o.items() if v not in (None, "", [], {})}
    if isinstance(o, list):
        return [_drop_nulls(v) for v in o]
    return o


def normalize_mcp_result(resp: Any) -> Any:
    """{'content':[{'type':'text','text':'{...json...}'}]} -> the parsed JSON (compact)."""
    if isinstance(resp, dict) and isinstance(resp.get("content"), list) and set(resp) <= {"content", "isError",
                                                                                          "structuredContent"}:
        if resp.get("structuredContent"):
            return resp["structuredContent"]
        texts = [c.get("text", "") for c in resp["content"] if isinstance(c, dict) and c.get("type") == "text"]
        if len(texts) == 1:
            try:
                return json.loads(texts[0])
            except (ValueError, TypeError):
                return {"text": texts[0]}
        if texts:
            parsed = []
            for t in texts:
                try:
                    parsed.append(json.loads(t))
                except (ValueError, TypeError):
                    parsed.append(t)
            return {"result": parsed}
    return resp


def _shrink(o: Any, depth: int = 0) -> Any:
    if isinstance(o, str):
        return o if len(o) <= MAX_STR else o[:MAX_STR] + "...(cut)"
    if isinstance(o, list):
        items = [_shrink(v, depth + 1) for v in o[:MAX_LIST_ITEMS]]
        if len(o) > MAX_LIST_ITEMS:
            items.append({"_truncated": f"+{len(o) - MAX_LIST_ITEMS} more items not shown; narrow the query"})
        return items
    if isinstance(o, dict):
        return {k: _shrink(v, depth + 1) for k, v in o.items()}
    return o


def compact_for_llm(resp: Any) -> Any:
    """Normalise + trim a tool response so the model sees the facts, not the formatting."""
    resp = _drop_nulls(normalize_mcp_result(resp))
    try:
        size = len(json.dumps(resp, default=str))
    except (TypeError, ValueError):
        return resp
    if size <= MAX_TOOL_CHARS:
        return resp
    return _shrink(resp)


def compact_history(llm_request) -> int:
    """Drop old images and bulky old tool results from the request. Returns approx chars saved."""
    contents = llm_request.contents or []
    saved = 0
    cutoff = max(0, len(contents) - KEEP_RECENT_CONTENTS)
    last_user = max((i for i, c in enumerate(contents) if c.role == "user" and
                     any(getattr(p, "text", None) for p in (c.parts or []))), default=-1)
    for i, content in enumerate(contents):
        for part in content.parts or []:
            if getattr(part, "inline_data", None) and part.inline_data.data and i != last_user and i < len(contents) - 1:
                saved += len(part.inline_data.data) // 4
                part.inline_data = None
                part.text = "[earlier image omitted - its findings were recorded]"
            elif i < cutoff and getattr(part, "function_response", None):
                resp = part.function_response.response
                text = json.dumps(resp, default=str)
                if len(text) > 400:
                    saved += len(text) - 80
                    part.function_response.response = {"summary": "[older tool result omitted to save tokens]"}
    return saved
