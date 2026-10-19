"""LangChain model backed by the locally authenticated Codex CLI."""
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool

from core.llm import codex_runtime


class ChatCodex(BaseChatModel):
    model_name: str = "gpt-6-astra"
    timeout: int = 600
    reasoning_effort: str = "medium"
    max_tokens: int = 8000

    @property
    def _llm_type(self):
        return "codex-local"

    @property
    def _identifying_params(self):
        return {"model_name": self.model_name, "reasoning_effort": self.reasoning_effort}

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        converted = [convert_to_openai_tool(t) for t in tools]
        if tool_choice is True or tool_choice == "any":
            tool_choice = "required"
        elif isinstance(tool_choice, str) and tool_choice not in {"auto", "none", "required"}:
            tool_choice = {"type": "function", "function": {"name": tool_choice}}
        return self.bind(tools=converted, tool_choice=tool_choice, **kwargs)

    def _request(self, messages, stop=None, **kwargs):
        if stop:
            raise ValueError("本机 Codex 模型不支持 stop 参数。")
        history = []
        roles = {"human": "user", "ai": "assistant", "system": "system", "tool": "tool"}
        for message in messages:
            item = {"role": roles.get(message.type, getattr(message, "role", message.type)),
                    "content": message.content}
            if getattr(message, "tool_calls", None):
                item["tool_calls"] = [{"id": t["id"], "name": t["name"], "arguments": t["args"]}
                                      for t in message.tool_calls]
            elif message.additional_kwargs.get("tool_calls"):
                item["tool_calls"] = message.additional_kwargs["tool_calls"]
            if getattr(message, "tool_call_id", None):
                item["tool_call_id"] = message.tool_call_id
            if message.name:
                item["name"] = message.name
            history.append(item)
        return dict(messages=history, tools=kwargs.get("tools"), model=self.model_name,
                    timeout=self.timeout, reasoning_effort=self.reasoning_effort,
                    max_tokens=kwargs.get("max_tokens", self.max_tokens),
                    tool_choice=kwargs.get("tool_choice"), response_format=kwargs.get("response_format"))

    def _result(self, result):
        usage = result.get("usage", {})
        input_tokens, output_tokens = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
        calls = [{"id": t["id"], "name": t["name"], "args": t["arguments"], "type": "tool_call"}
                 for t in result["tool_calls"]]
        message = AIMessage(content=result["content"], tool_calls=calls,
                            usage_metadata={"input_tokens": input_tokens, "output_tokens": output_tokens,
                                            "total_tokens": input_tokens + output_tokens},
                            response_metadata={"model_name": self.model_name, "provider": "codex",
                                               "finish_reason": "tool_calls" if calls else "stop"})
        return ChatResult(generations=[ChatGeneration(message=message)], llm_output={
            "model_name": self.model_name, "token_usage": {
                "prompt_tokens": input_tokens, "completion_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            }})

    def _generate(self, messages, stop=None, run_manager=None, **kwargs: Any):
        return self._result(codex_runtime.complete(**self._request(messages, stop, **kwargs)))

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs: Any):
        return self._result(await codex_runtime.acomplete(**self._request(messages, stop, **kwargs)))
