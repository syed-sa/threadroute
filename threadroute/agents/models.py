"""Gemini model construction with transport-level retries. Model-level fallback lives in callbacks."""

from __future__ import annotations

from google.adk.models.google_llm import Gemini
from google.genai import types

RETRY_STATUS = [408, 429, 500, 502, 503, 504]


def build_model(name: str) -> Gemini:
    return Gemini(
        model=name,
        retry_options=types.HttpRetryOptions(
            attempts=3, initial_delay=1.0, max_delay=8.0, exp_base=2.0, http_status_codes=RETRY_STATUS),
    )
