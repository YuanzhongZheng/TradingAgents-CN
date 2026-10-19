import asyncio
import json
import sys
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import tool

from core.llm import codex_runtime as runtime
from core.llm.models import LLMConfig, Message
from core.llm.providers.codex import CodexAdapter
from tradingagents.llm_adapters.codex_adapter import ChatCodex


TOOL = {"type": "function", "function": {"name": "get_data", "parameters": {
    "type": "object", "properties": {"symbol": {"type": "string"}},
    "required": ["symbol"], "additionalProperties": False,
}}}


def events(content="分析结果", calls=None, completed=True):
    result = [{"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps({
        "content": content, "tool_calls": calls or [],
    })}}]
    if completed:
        result.append({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}})
    return "\n".join(json.dumps(e) for e in result)


def test_protocol_requires_completed_turn():
    with pytest.raises(runtime.CodexError):
        runtime.parse_result(events(completed=False), "", 0, [])
    with pytest.raises(runtime.CodexError):
        runtime.parse_result(events() + '\n{"type":"turn.failed"}', "", 0, [])


@pytest.mark.parametrize("call", [
    {"name": "other", "arguments_json": '{}'},
    {"name": "get_data", "arguments_json": '[]'},
    {"name": "get_data", "arguments_json": '{"symbol": 123}'},
    {"name": "get_data", "arguments_json": '{}'},
    {"name": "get_data", "arguments_json": 'not JSON'},
])
def test_invalid_tool_requests_are_never_executed(call):
    with pytest.raises(runtime.CodexError):
        runtime.parse_result(events(calls=[call]), "", 0, [TOOL])


def test_tool_choices_and_structured_output():
    with pytest.raises(runtime.CodexError):
        runtime.parse_result(events(), "", 0, [TOOL], "required")
    with pytest.raises(runtime.CodexError):
        runtime.parse_result(events(calls=[{"name": "get_data", "arguments_json": '{"symbol":"A"}'}]),
                             "", 0, [TOOL], "none")
    with pytest.raises(runtime.CodexError):
        runtime.parse_result(events("not JSON"), "", 0, [], response_format={"type": "json_object"})
    assert runtime.parse_result(events('{"ok":true}'), "", 0, [],
                                response_format={"type": "json_object"})["content"] == '{"ok":true}'


def test_cli_uses_login_not_api_key_and_private_stdin(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-passed")
    monkeypatch.setenv("CODEX_API_KEY", "must-not-be-passed")
    monkeypatch.setenv("MONGO_PASSWORD", "must-not-be-passed")
    monkeypatch.setattr(runtime, "executable", lambda: "/fake/codex")
    calls = []
    def fake_process(args, **kwargs):
        calls.append((args, kwargs))
        if args[1] == "login":
            return 0, "", "Logged in using ChatGPT"
        assert "private input" not in str(args)
        assert b"private input" in kwargs["data"]
        assert "--ignore-user-config" in args and "read-only" in args
        return 0, events(), ""
    monkeypatch.setattr(runtime, "_process", fake_process)
    assert runtime.complete([{"role": "user", "content": "private input"}])["content"] == "分析结果"
    assert len(calls) == 2
    for _, kwargs in calls:
        assert not {"OPENAI_API_KEY", "CODEX_API_KEY", "MONGO_PASSWORD"} & kwargs["env"].keys()


def test_rejects_api_key_login(monkeypatch):
    monkeypatch.setattr(runtime, "executable", lambda: "/fake/codex")
    monkeypatch.setattr(runtime, "_process", lambda *a, **k: (0, "Logged in using an API key", ""))
    with pytest.raises(runtime.CodexError, match="ChatGPT"):
        runtime.complete([])


def test_timeout_reaps_process(tmp_path):
    with pytest.raises(runtime.CodexError, match="超时"):
        runtime._process([sys.executable, "-c", "import time; time.sleep(10)"],
                         cwd=tmp_path, env=runtime.cli_environment(), timeout=0.1)


def test_cancelled_call_stops_process(tmp_path):
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(runtime.CodexError, match="取消"):
        runtime._process([sys.executable, "-c", "import time; time.sleep(10)"],
                         cwd=tmp_path, env=runtime.cli_environment(), timeout=5, cancelled=cancelled)


def test_langchain_tool_roundtrip(monkeypatch):
    @tool
    def get_data(symbol: str) -> str:
        """Return fixture financial data."""
        return "revenue=120; previous_revenue=100"
    requests = []
    def fake_complete(**request):
        requests.append(request)
        if len(requests) == 1:
            return {"content": "", "tool_calls": [{"id": "c1", "name": "get_data", "arguments": {"symbol": "A"}}],
                    "usage": {"input_tokens": 10, "output_tokens": 5}}
        assert request["messages"][-1]["tool_call_id"] == "c1"
        assert request["messages"][-2]["tool_calls"][0]["arguments"] == {"symbol": "A"}
        assert "revenue=120" in request["messages"][-1]["content"]
        return {"content": "营收同比增长20%", "tool_calls": [], "usage": {"input_tokens": 20, "output_tokens": 10}}
    monkeypatch.setattr(runtime, "complete", fake_complete)
    llm = ChatCodex().bind_tools([get_data])
    history = [HumanMessage(content="分析 A 的营收")]
    first = llm.invoke(history)
    assert first.tool_calls[0]["name"] == "get_data"
    value = get_data.invoke(first.tool_calls[0]["args"])
    final = llm.invoke(history + [first, ToolMessage(content=value, tool_call_id="c1")])
    assert final.content == "营收同比增长20%"
    assert final.usage_metadata["total_tokens"] == 30


def test_unified_adapter_async_and_usage(monkeypatch):
    async def fake_complete(**request):
        assert request["reasoning_effort"] == "high"
        return {"content": "报告", "tool_calls": [], "usage": {"input_tokens": 7, "output_tokens": 2}}
    monkeypatch.setattr(runtime, "acomplete", fake_complete)
    adapter = CodexAdapter(LLMConfig(provider="codex", model="gpt-6-astra", reasoning_effort="high"))
    result = asyncio.run(adapter.achat([Message(role="user", content="分析")]))
    assert result.content == "报告"
    assert result.usage["total_tokens"] == 9


def test_factory_uses_codex_without_key():
    from tradingagents.graph.trading_graph import create_llm_by_provider
    llm = create_llm_by_provider("codex", "gpt-6-astra", "", 0.2, 8000, 600)
    assert isinstance(llm, ChatCodex)
    assert llm.timeout == 600


def test_cancelled_queued_call_never_starts_cli(monkeypatch):
    cancelled = threading.Event()
    cancelled.set()
    process = Mock()
    monkeypatch.setattr(runtime, "_process", process)
    with pytest.raises(runtime.CodexError, match="取消"):
        runtime.complete([], cancelled=cancelled)
    process.assert_not_called()


@pytest.mark.parametrize("selection", ["gpt-6-astra", "codex::gpt-6-astra"])
def test_analysis_selection_preserves_codex_parameters(monkeypatch, selection):
    from core.workflow.builder import LegacyDependencyProvider

    model_config = {"provider": "codex", "model_name": "gpt-6-astra",
                    "timeout": 900, "max_tokens": 12000, "reasoning_effort": "high"}
    collection = SimpleNamespace(find_one=lambda *a, **k: {"llm_configs": [model_config]})
    db = SimpleNamespace(system_configs=collection)
    client = Mock()
    client.__getitem__ = Mock(return_value=db)
    monkeypatch.setattr("pymongo.MongoClient", lambda *a, **k: client)
    monkeypatch.setattr(
        "app.services.llm_model_resolution.get_provider_and_url_by_model_sync",
        lambda *a, **k: {"provider": "codex", "backend_url": "", "api_key": None},
    )
    monkeypatch.setattr("app.core.jdyun.is_jdyun_mode", lambda: False)
    provider = LegacyDependencyProvider({"quick_think_llm": selection, "deep_think_llm": selection})
    for role in ["quick", "deep"]:
        llm = provider.get_llm(role)
        assert isinstance(llm, ChatCodex)
        assert llm.model_name == "gpt-6-astra"
        assert llm.reasoning_effort == "high"
        assert llm.timeout == 900
        assert llm.max_tokens == 12000


def test_disabled_local_provider_does_not_register(monkeypatch):
    from app.services.codex_config_service import ensure_codex_config
    monkeypatch.setenv("CODEX_LOCAL_ENABLED", "false")
    db = SimpleNamespace(llm_providers=SimpleNamespace(update_one=AsyncMock()))
    asyncio.run(ensure_codex_config(db))
    db.llm_providers.update_one.assert_not_called()


@pytest.mark.parametrize("selection", ["gpt-6-astra", "codex::gpt-6-astra"])
def test_database_failure_never_routes_astra_to_paid_api(monkeypatch, selection):
    from app.services.llm_model_resolution import get_provider_and_url_by_model_sync
    monkeypatch.setattr("pymongo.MongoClient", Mock(side_effect=RuntimeError("offline")))
    result = get_provider_and_url_by_model_sync(selection)
    assert result == {"provider": "codex", "backend_url": "", "api_key": None}


def test_existing_agent_executes_astra_tool_requests(monkeypatch):
    from core.agents.base import BaseAgent

    class Analyst(BaseAgent):
        agent_id = "astra_fixture"

        def execute(self, state):
            return state

    executed = []

    @tool
    def get_data(symbol: str) -> str:
        """Return synthetic revenue for a stock."""
        executed.append(symbol)
        return "revenue=120; previous_revenue=100"

    def fake_complete(**request):
        if request["tools"]:
            return {"content": "", "tool_calls": [{"id": "c1", "name": "get_data",
                                                      "arguments": {"symbol": "TEST"}}]}
        tool_results = [m for m in request["messages"] if m["role"] == "tool"]
        assert tool_results[0]["tool_call_id"] == "c1"
        assert "revenue=120" in tool_results[0]["content"]
        return {"content": "测试数据营收增长20%。", "tool_calls": []}

    monkeypatch.setattr(runtime, "complete", fake_complete)
    analyst = Analyst(llm=ChatCodex())
    analyst._langchain_tools = [get_data]
    assert analyst.invoke_with_tools([HumanMessage(content="分析 TEST")]) == "测试数据营收增长20%。"
    assert executed == ["TEST"]
