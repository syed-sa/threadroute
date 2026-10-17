"""ADK callbacks that make the agent layer self-healing and feedback-aware.

  on_model_error  -> walk the configured fallback models, then degrade gracefully (never crash a chat)
  on_tool_error   -> turn tool exceptions into structured results the model can recover from
  after_tool      -> surface 'degraded' (fallback) data so the agent tells the user about it
  make_instruction-> inject learned guidance (feedback loop) into every agent turn
"""

from __future__ import annotations

from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from threadroute.agents.models import build_model
from threadroute.config import get_settings
from threadroute.feedback import store as fb
from threadroute.resilience import store as rstore


def _text_response(text: str) -> LlmResponse:
    return LlmResponse(content=types.Content(role="model", parts=[types.Part(text=text)]))


async def on_model_error(callback_context: CallbackContext, llm_request: LlmRequest,
                         error: Exception) -> LlmResponse | None:
    settings = get_settings()
    agent = callback_context.agent_name
    rstore.log_event("llm", llm_request.model, "failure", f"{agent}: {type(error).__name__}: {str(error)[:200]}")
    for fallback in settings.fallback_models:
        if fallback == llm_request.model:
            continue
        try:
            request = llm_request.model_copy(update={"model": fallback})
            last = None
            async for response in build_model(fallback).generate_content_async(request, stream=False):
                last = response
            if last is not None:
                rstore.log_event("llm", fallback, "model_fallback",
                                 f"{agent}: '{llm_request.model}' failed, answered by '{fallback}'")
                return last
        except Exception as exc:  # noqa: BLE001 - try the next fallback
            rstore.log_event("llm", fallback, "failure", f"fallback failed: {type(exc).__name__}: {str(exc)[:160]}")
    rstore.log_event("llm", None, "exhausted", f"{agent}: all models failed")
    return _text_response(
        "I could not reach the AI model just now (every configured model failed). Nothing was changed. "
        "Please try again in a moment - run `python -m threadroute doctor` if this keeps happening.")


def on_tool_error(tool: BaseTool, args: dict[str, Any], tool_context: ToolContext,
                  error: Exception) -> dict[str, Any]:
    rstore.log_event("tools", tool.name, "tool_error",
                     f"{tool_context.agent_name}: {type(error).__name__}: {str(error)[:200]} args={str(args)[:120]}")
    return {
        "status": "error",
        "tool": tool.name,
        "error": f"{type(error).__name__}: {str(error)[:300]}",
        "hint": ("Check the arguments (ids must exist, enum values must match) and retry once. If it fails "
                 "again, tell the user the data source is unavailable instead of guessing."),
    }


def after_tool(tool: BaseTool, args: dict[str, Any], tool_context: ToolContext,
               tool_response: Any) -> dict | None:
    if isinstance(tool_response, dict) and tool_response.get("degraded"):
        notes = tool_context.state.get("degraded_notes", [])
        note = f"{tool.name}: {tool_response.get('healing_note', 'served by fallback source')}"
        if note not in notes:
            tool_context.state["degraded_notes"] = [*notes, note]
    return None


def _user_text(ctx: ReadonlyContext) -> str:
    content = getattr(ctx, "user_content", None)
    if content and content.parts:
        return " ".join(p.text for p in content.parts if getattr(p, "text", None))
    return ""


def make_instruction(agent_name: str, base_prompt: str):
    """Instruction provider: the base prompt + learned guidance relevant to this agent and request."""
    scenarios = fb.AGENT_SCENARIOS.get(agent_name, ["global"])

    def provider(ctx: ReadonlyContext) -> str:
        try:
            rules = fb.retrieve(scenarios, _user_text(ctx))
        except Exception:  # noqa: BLE001 - feedback must never take an agent down
            rules = []
        return base_prompt + fb.format_guidance_block(rules)

    return provider
