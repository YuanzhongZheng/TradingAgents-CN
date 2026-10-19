from app.services.assistant.tools import select_assistant_tools


def _tool(name: str, description: str) -> dict:
    return {"type": "function", "function": {"name": name, "description": description}}


def test_small_tool_list_is_kept():
    tools = [_tool("web_search", "搜索网页")]
    assert select_assistant_tools(tools, "贵州茅台股价") is tools


def test_large_tool_list_keeps_core_and_matching_tools():
    tools = [_tool(f"noise_{index}", "无关的期权定价模型") for index in range(70)]
    tools.append(_tool("get_stock_market_data_unified", "股票行情K线"))
    tools.append(_tool("dongchedi_sales_rank", "懂车帝车型销量排行，查询问界各车型月度销量"))

    selected = select_assistant_tools(tools, "问界各车型最近销量")
    names = [item["function"]["name"] for item in selected]

    assert "get_stock_market_data_unified" in names
    assert "dongchedi_sales_rank" in names
    assert len(selected) < len(tools)
    assert not any(name.startswith("noise_") for name in names)
