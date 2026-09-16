"""
Thin provider abstraction so the discovery loop in llm_agent.py doesn't care
whether the underlying model is called through Anthropic's Messages API or
an OpenAI-compatible Chat Completions endpoint (used for Google's Gemini
free tier). Both providers expose the same two calls:

  provider.observe(system_prompt, snapshot_text, tools) -> ToolCall
  provider.report_result(tool_call, result_text)

Each provider owns its own native conversation history internally.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]
    text: Optional[str] = None  # any reasoning/prose the model emitted alongside the tool call


class LLMProvider:
    def observe(self, system_prompt: str, snapshot_text: str, tools: list[dict]) -> Optional[ToolCall]:
        raise NotImplementedError

    def report_result(self, tool_call: ToolCall, result_text: str) -> None:
        raise NotImplementedError


class AnthropicProvider(LLMProvider):
    def __init__(self, model: str):
        import anthropic
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model
        self.messages: list[dict] = []

    def observe(self, system_prompt, snapshot_text, tools):
        self.messages.append({"role": "user", "content": snapshot_text})
        response = self.client.messages.create(
            model=self.model, max_tokens=1024, system=system_prompt,
            tools=tools, messages=self.messages,
        )
        self.messages.append({"role": "assistant", "content": response.content})
        tool_use = next((b for b in response.content if b.type == "tool_use"), None)
        text_block = next((b for b in response.content if b.type == "text"), None)
        text = text_block.text if text_block else None
        if tool_use is None:
            self.messages.append({"role": "user", "content": "Please call a tool."})
            return None
        return ToolCall(id=tool_use.id, name=tool_use.name, input=tool_use.input, text=text)

    def report_result(self, tool_call: ToolCall, result_text: str) -> None:
        self.messages.append({
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_call.id, "content": result_text}],
        })


class OpenAICompatProvider(LLMProvider):
    """Works against any OpenAI Chat Completions-compatible endpoint with
    function-calling support. Used here for Google Gemini's free-tier
    OpenAI-compatible endpoint, but not Gemini-specific."""

    def __init__(self, model: str, base_url: str, api_key_env: str):
        from openai import OpenAI
        api_key = os.environ.get(api_key_env)
        if not api_key:
            raise RuntimeError(f"{api_key_env} is not set")
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model = model
        self.messages: list[dict] = []
        self._system_sent = False

    @staticmethod
    def _to_openai_tools(tools: list[dict]) -> list[dict]:
        return [
            {"type": "function", "function": {
                "name": t["name"], "description": t["description"], "parameters": t["input_schema"],
            }}
            for t in tools
        ]

    def observe(self, system_prompt, snapshot_text, tools):
        if not self._system_sent:
            self.messages.append({"role": "system", "content": system_prompt})
            self._system_sent = True
        self.messages.append({"role": "user", "content": snapshot_text})
        response = self._create_with_retry(tools)
        choice = response.choices[0].message
        self.messages.append(choice.model_dump(exclude_none=True))
        if not choice.tool_calls:
            self.messages.append({"role": "user", "content": "Please call a tool."})
            return None
        call = choice.tool_calls[0]
        try:
            args = json.loads(call.function.arguments) if call.function.arguments else {}
        except json.JSONDecodeError:
            args = {}
        return ToolCall(id=call.id, name=call.function.name, input=args, text=choice.content)

    def report_result(self, tool_call: ToolCall, result_text: str) -> None:
        self.messages.append({"role": "tool", "tool_call_id": tool_call.id, "content": result_text})

    def _create_with_retry(self, tools: list[dict], max_retries: int = 8, base_delay: float = 15.0):
        """Free-tier endpoints (e.g. Gemini's free quota) rate-limit aggressively.
        Retry on 429 with a fixed backoff long enough to clear a per-minute quota
        window, rather than failing the whole discovery run over a transient cap."""
        import openai as openai_sdk
        last_exc = None
        for attempt in range(max_retries):
            try:
                return self.client.chat.completions.create(
                    model=self.model, messages=self.messages, tools=self._to_openai_tools(tools),
                    tool_choice="auto", max_tokens=4096,
                )
            except openai_sdk.RateLimitError as exc:
                last_exc = exc
                time.sleep(base_delay)
        raise last_exc


def build_provider(provider_name: str, model: str) -> LLMProvider:
    provider_name = provider_name.lower()
    if provider_name == "anthropic":
        return AnthropicProvider(model)
    if provider_name == "gemini":
        return OpenAICompatProvider(
            model=model,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            api_key_env="GEMINI_API_KEY",
        )
    if provider_name == "openai":
        return OpenAICompatProvider(model=model, base_url="https://api.openai.com/v1", api_key_env="OPENAI_API_KEY")
    raise ValueError(f"unknown LLM_PROVIDER {provider_name!r}")
