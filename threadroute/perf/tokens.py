"""Token ledger + cost model + budget guard (all in memory; reads usage reported by the model).

* `record_usage` is called from the model callbacks with Gemini's `usage_metadata` (real numbers when a
  live model answers). For the scripted offline model it records a clearly-labelled *estimate*
  (characters / 4) so the benchmark can still compare prompt sizes before/after optimisation.
* `cost_usd` multiplies tokens by config/pricing.yaml (placeholder prices - you confirm them).
* `check_budget` lets callbacks stop runaway sessions/loops gracefully instead of burning tokens.
"""

from __future__ import annotations

import os
import threading
from collections import defaultdict
from dataclasses import dataclass, field

import yaml

from threadroute.config import REPO_ROOT

DEFAULT_SESSION_TOKEN_BUDGET = 250_000
DEFAULT_MAX_MODEL_CALLS_PER_TURN = 14


@dataclass
class Usage:
    calls: int = 0
    prompt: int = 0
    output: int = 0
    cached: int = 0
    thinking: int = 0
    estimated_calls: int = 0

    def add(self, other: "Usage") -> None:
        for f in ("calls", "prompt", "output", "cached", "thinking", "estimated_calls"):
            setattr(self, f, getattr(self, f) + getattr(other, f))

    @property
    def total(self) -> int:
        return self.prompt + self.output + self.thinking


_pricing_cache: dict | None = None


def pricing() -> dict:
    global _pricing_cache
    if _pricing_cache is None:
        path = os.environ.get("THREADROUTE_PRICING") or (REPO_ROOT / "config" / "pricing.yaml")
        try:
            with open(path, encoding="utf-8") as fh:
                _pricing_cache = yaml.safe_load(fh) or {}
        except OSError:
            _pricing_cache = {"default": {"input": 0.0, "output": 0.0, "cached_input": 0.0}, "models": {}}
    return _pricing_cache


def cost_usd(model: str, u: Usage) -> float:
    p = pricing()
    rates = (p.get("models") or {}).get(model) or p.get("default") or {}
    fresh_in = max(0, u.prompt - u.cached)
    return (fresh_in * rates.get("input", 0) + u.cached * rates.get("cached_input", 0)
            + (u.output + u.thinking) * rates.get("output", 0)) / 1_000_000


class Ledger:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.by_agent: dict[tuple[str, str], Usage] = defaultdict(Usage)  # (agent, model)
        self.by_session: dict[str, Usage] = defaultdict(Usage)
        self.by_turn_calls: dict[str, int] = defaultdict(int)  # invocation_id -> model calls

    def record(self, agent: str, model: str, session_id: str, u: Usage) -> None:
        with self._lock:
            self.by_agent[(agent, model)].add(u)
            self.by_session[session_id].add(u)

    def note_call(self, invocation_id: str) -> int:
        with self._lock:
            self.by_turn_calls[invocation_id] += 1
            if len(self.by_turn_calls) > 5000:  # bounded memory
                for k in list(self.by_turn_calls)[:2500]:
                    self.by_turn_calls.pop(k, None)
            return self.by_turn_calls[invocation_id]

    def session_total(self, session_id: str) -> int:
        return self.by_session[session_id].total if session_id in self.by_session else 0

    def snapshot(self) -> dict:
        with self._lock:
            agents = [{"agent": a, "model": m, "calls": u.calls, "prompt_tokens": u.prompt,
                       "output_tokens": u.output, "cached_tokens": u.cached, "thinking_tokens": u.thinking,
                       "estimated_calls": u.estimated_calls, "cost_usd": round(cost_usd(m, u), 6)}
                      for (a, m), u in sorted(self.by_agent.items())]
        total = Usage()
        cost = 0.0
        for row in agents:
            total.add(Usage(row["calls"], row["prompt_tokens"], row["output_tokens"], row["cached_tokens"],
                            row["thinking_tokens"], row["estimated_calls"]))
            cost += row["cost_usd"]
        return {"agents": agents, "total": {"calls": total.calls, "prompt_tokens": total.prompt,
                                            "output_tokens": total.output, "cached_tokens": total.cached,
                                            "thinking_tokens": total.thinking, "cost_usd": round(cost, 6),
                                            "estimated_calls": total.estimated_calls},
                "currency": pricing().get("currency", "USD"),
                "note": "Prices come from config/pricing.yaml (placeholders - confirm them)."}

    def reset(self) -> None:
        with self._lock:
            self.by_agent.clear()
            self.by_session.clear()
            self.by_turn_calls.clear()


ledger = Ledger()


def session_budget() -> int:
    return int(os.environ.get("THREADROUTE_SESSION_TOKEN_BUDGET", DEFAULT_SESSION_TOKEN_BUDGET))


def max_calls_per_turn() -> int:
    return int(os.environ.get("THREADROUTE_MAX_MODEL_CALLS_PER_TURN", DEFAULT_MAX_MODEL_CALLS_PER_TURN))


def usage_from_response(llm_response, llm_request=None) -> Usage:
    """Real usage when the model reports it, else a chars/4 estimate (flagged)."""
    meta = getattr(llm_response, "usage_metadata", None)
    if meta is not None and getattr(meta, "total_token_count", None):
        return Usage(calls=1, prompt=meta.prompt_token_count or 0, output=meta.candidates_token_count or 0,
                     cached=getattr(meta, "cached_content_token_count", 0) or 0,
                     thinking=getattr(meta, "thoughts_token_count", 0) or 0)
    prompt_chars = 0
    if llm_request is not None:
        for content in llm_request.contents or []:
            for part in content.parts or []:
                if getattr(part, "text", None):
                    prompt_chars += len(part.text)
                elif getattr(part, "function_response", None):
                    prompt_chars += len(str(part.function_response.response))
                elif getattr(part, "function_call", None):
                    prompt_chars += len(str(part.function_call.args))
                elif getattr(part, "inline_data", None) and part.inline_data.data:
                    prompt_chars += 1100  # ~ a few hundred image tokens
        si = getattr(getattr(llm_request, "config", None), "system_instruction", None)
        if si:
            prompt_chars += len(str(si))
    out_chars = 0
    content = getattr(llm_response, "content", None)
    if content:
        for part in content.parts or []:
            if getattr(part, "text", None):
                out_chars += len(part.text)
            elif getattr(part, "function_call", None):
                out_chars += len(str(part.function_call.args)) + 20
    return Usage(calls=1, prompt=prompt_chars // 4, output=out_chars // 4, estimated_calls=1)
