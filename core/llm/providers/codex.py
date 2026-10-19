"""Codex transport for the unified assistant/planner client."""
from .. import codex_runtime
from ..models import LLMResponse, ToolCall
from .base import BaseAdapter


class CodexAdapter(BaseAdapter):
    def initialize(self):
        codex_runtime.executable()

    def validate_config(self):
        return self.config.model == "gpt-6-astra"

    def _request(self, messages, tools=None, **kwargs):
        return dict(messages=[m.model_dump(exclude_none=True) for m in messages], tools=tools,
                    model=self.config.model, timeout=self.config.timeout,
                    reasoning_effort=self.config.reasoning_effort or "medium",
                    max_tokens=kwargs.get("max_tokens", self.config.max_tokens),
                    tool_choice=kwargs.get("tool_choice", self.config.tool_choice),
                    response_format=kwargs.get("response_format"))

    def _response(self, result):
        raw = result.get("usage", {})
        usage = {"prompt_tokens": raw.get("input_tokens", 0),
                 "completion_tokens": raw.get("output_tokens", 0)}
        usage["total_tokens"] = sum(usage.values())
        return LLMResponse(content=result["content"],
                           tool_calls=[ToolCall(**t) for t in result["tool_calls"]],
                           model=self.config.model, provider=self.config.provider, usage=usage,
                           finish_reason="tool_calls" if result["tool_calls"] else "stop")

    def chat(self, messages, tools=None, **kwargs):
        return self._response(codex_runtime.complete(**self._request(messages, tools, **kwargs)))

    async def achat(self, messages, tools=None, **kwargs):
        return self._response(await codex_runtime.acomplete(**self._request(messages, tools, **kwargs)))

    async def astream(self, messages, tools=None, **kwargs):
        response = await self.achat(messages, tools, **kwargs)
        if response.tool_calls:
            raise ValueError("带工具调用请使用 achat；文本输出接口不能执行工具。")
        yield response.content or ""

    def chat_stream_deltas(self, messages, tools=None, **kwargs):
        response = self.chat(messages, tools, **kwargs)
        if response.tool_calls:
            raise ValueError("带工具调用请使用 chat；文本输出接口不能执行工具。")
        yield {"type": "content", "text": response.content or ""}
