"""A scripted stand-in for Gemini so the *whole* agent stack (ADK runner, agent transfers, MCP tools,
approval gate, callbacks) can be exercised in tests and offline demos with no API key."""

from __future__ import annotations

from typing import Any, AsyncGenerator

from google.adk.models.base_llm import BaseLlm, LlmCapabilities
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types


def say(text: str) -> LlmResponse:
    return LlmResponse(content=types.Content(role="model", parts=[types.Part(text=text)]))


def call(name: str, **args: Any) -> LlmResponse:
    return LlmResponse(content=types.Content(
        role="model", parts=[types.Part(function_call=types.FunctionCall(name=name, args=args))]))


def transfer(agent_name: str) -> LlmResponse:
    return call("transfer_to_agent", agent_name=agent_name)


class ScriptedLlm(BaseLlm):
    """Returns the queued responses in order, whichever agent asks. Records every request it sees."""

    model: str = "scripted"
    script: list[Any] = []
    seen: list[Any] = []

    @property
    def capabilities(self) -> LlmCapabilities:
        return LlmCapabilities(output_schema_and_tools=False)

    @classmethod
    def supported_models(cls) -> list[str]:
        return ["scripted"]

    async def generate_content_async(self, llm_request: LlmRequest, stream: bool = False
                                     ) -> AsyncGenerator[LlmResponse, None]:
        self.seen.append(llm_request)
        item = self.script.pop(0) if self.script else say("(scripted model has no more responses)")
        if callable(item):  # a function of the request, for assertions or dynamic replies
            item = item(llm_request)
        yield item
