from app.services.assistant.tools import (
    reply_requests_missing_tool,
    select_assistant_tools,
)


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


def _names(selected):
    return [item["function"]["name"] for item in selected]


def test_short_followup_keeps_export_tool():
    tools = [_tool(f"noise_{index}", "无关的期权定价模型") for index in range(70)]
    tools.append(_tool("export_table_excel_tool", "把对话中已经产生的表格导出为 Excel 文件"))
    selected = select_assistant_tools(tools, "好的，导出吧")
    assert "export_table_excel_tool" in _names(selected)


def test_history_and_previously_used_tools_stay_available():
    tools = [_tool(f"noise_{index}", "无关的期权定价模型") for index in range(70)]
    tools.append(_tool("export_table_excel_tool", "把预测结果导出成 Excel"))
    tools.append(_tool("custom_zebra_tool", "斑马纹样统计"))
    selected = select_assistant_tools(
        tools,
        "好的",
        history_text="请把预测结果导出成Excel",
        used_tool_names=["custom_zebra_tool"],
    )
    names = _names(selected)
    assert "export_table_excel_tool" in names
    assert "custom_zebra_tool" in names


def test_recover_keeps_a_single_word_match():
    tools = [_tool(f"noise_{index}", "无关的期权定价模型") for index in range(70)]
    tools.append(_tool("zebra_report", "斑马"))
    message = "请整理上周会议纪要里的行动项并写成备忘"
    missed = select_assistant_tools(tools, message, history_text="斑马")
    recovered = select_assistant_tools(tools, message, history_text="斑马", recover=True)
    assert "zebra_report" not in _names(missed)
    assert "zebra_report" in _names(recovered)


def test_missing_tool_reply_is_detected():
    assert reply_requests_missing_tool("当前工具无法完成导出")
    assert not reply_requests_missing_tool("表格已经导出")
