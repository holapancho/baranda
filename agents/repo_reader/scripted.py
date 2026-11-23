"""A fake model that plays a fixed script. Used by tests and the offline demo; no API key needed."""

from __future__ import annotations

from typing import Any, AsyncGenerator

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import PrivateAttr

# A step is ("call", (tool_name, args)) or ("text", answer).
Step = tuple[str, Any]


class ScriptedLlm(BaseLlm):
    model: str = "scripted"
    script: list[Step] = []
    _turn: int = PrivateAttr(default=0)
    _requests: list[LlmRequest] = PrivateAttr(default_factory=list)

    @property
    def requests(self) -> list[LlmRequest]:
        return self._requests

    def reset(self, script: list[Step]) -> None:
        self.script = script
        self._turn = 0

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        self._requests.append(llm_request)
        kind, payload = self.script[min(self._turn, len(self.script) - 1)]
        self._turn += 1
        if kind == "call":
            name, args = payload
            part = types.Part.from_function_call(name=name, args=args)
        else:
            part = types.Part.from_text(text=payload)
        usage = types.GenerateContentResponseUsageMetadata(
            prompt_token_count=0, candidates_token_count=0, total_token_count=0
        )
        yield LlmResponse(content=types.Content(role="model", parts=[part]), usage_metadata=usage)
