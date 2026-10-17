"""Optional LLM-as-judge (a second opinion on groundedness). Runs only when asked (`evaluate --judge`).

The judge sees the question, the evidence the tools returned and the answer, and lists which claims are
supported / unsupported / contradicted. `agreement()` compares it with the deterministic checker so you can
see how far the two methods agree. `llm` is injectable so tests need no network."""

from __future__ import annotations

import json
from typing import Callable

from threadroute.config import get_settings

RUBRIC = """You are a strict fact-checker for a logistics assistant. You get a QUESTION, the EVIDENCE (the only data the
assistant retrieved) and the ANSWER. List every factual claim in the ANSWER (ids, quantities, money, dates, statuses,
actions) and classify each as supported (stated in the evidence), unsupported (not in the evidence), or contradicted
(the evidence says otherwise). Claims about what the assistant itself can or cannot do are not factual claims about the
world. Reply with JSON only: {"supported": [..], "unsupported": [..], "contradicted": [..], "verdict": "grounded"|"ungrounded"}"""


def _default_llm(prompt: str) -> str:
    from google import genai
    from google.genai import types

    s = get_settings()
    client = genai.Client()
    model = (s.fallback_models[-1] if s.fallback_models else s.model)  # a cheaper model is plenty for judging
    out = client.models.generate_content(model=model, contents=prompt, config=types.GenerateContentConfig(
        temperature=0, response_mime_type="application/json"))
    return out.text or "{}"


def judge_answer(question: str, evidence: list[dict], answer: str, llm: Callable[[str], str] | None = None) -> dict:
    ev = json.dumps(evidence, default=str)[:6000]
    prompt = f"{RUBRIC}\n\nQUESTION:\n{question}\n\nEVIDENCE:\n{ev}\n\nANSWER:\n{answer}\n"
    try:
        data = json.loads((llm or _default_llm)(prompt))
    except Exception as exc:  # noqa: BLE001 - a judge failure must not break an evaluation run
        return {"error": f"{type(exc).__name__}", "verdict": "unknown", "supported": [], "unsupported": [],
                "contradicted": []}
    data.setdefault("verdict", "grounded" if not data.get("unsupported") and not data.get("contradicted") else "ungrounded")
    return data


def agreement(deterministic_grounded: bool, judge: dict) -> bool | None:
    if judge.get("verdict") not in ("grounded", "ungrounded"):
        return None
    return (judge["verdict"] == "grounded") == deterministic_grounded
