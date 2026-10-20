"""助理操作工具模块（Assistant Ops）

提供分析触发、报告查询、定时任务管理、股票关注列表管理等操作类工具，
使智能助理从"数据教练"升级为"全能操作员"。

工具通过 @register_tool 装饰器自动注册到 ToolRegistry。
用户身份通过 core.tools.context 的 contextvars 机制获取。

按需导入：导入包本身不会加载工具实现；全量发现请用 ToolRegistry.list_all()。
"""

from core.tools._lazy import resolve_export

_EXPORTS = {
    'analysis_trigger': ('core.tools.implementations.assistant_ops.analysis_trigger', None),
    'report_query': ('core.tools.implementations.assistant_ops.report_query', None),
    'schedule_manager': ('core.tools.implementations.assistant_ops.schedule_manager', None),
    'watchlist_manager': ('core.tools.implementations.assistant_ops.watchlist_manager', None),
    'favorite_manager': ('core.tools.implementations.assistant_ops.favorite_manager', None),
    'position_analysis': ('core.tools.implementations.assistant_ops.position_analysis', None),
    'trade_review_ops': ('core.tools.implementations.assistant_ops.trade_review_ops', None),
    'risk_alerts': ('core.tools.implementations.assistant_ops.risk_alerts', None),
    'topic_manager': ('core.tools.implementations.assistant_ops.topic_manager', None),
    'recent_search': ('core.tools.implementations.assistant_ops.recent_search', None),
    'agent_executor': ('core.tools.implementations.assistant_ops.agent_executor', None),
    'stock_sync_ops': ('core.tools.implementations.assistant_ops.stock_sync_ops', None),
    'web_search': ('core.tools.implementations.assistant_ops.web_search', None),
    'fetch_url': ('core.tools.implementations.assistant_ops.fetch_url', None),
    'set_stop_gain_alert': ('.gain_alerts', 'set_stop_gain_alert'),
    'list_stop_gain_alerts': ('.gain_alerts', 'list_stop_gain_alerts'),
    'clear_stop_gain_alert': ('.gain_alerts', 'clear_stop_gain_alert'),
    'user_state': ('core.tools.implementations.assistant_ops.user_state', None),
    'system_status': ('core.tools.implementations.assistant_ops.system_status', None),
    'user_activity': ('core.tools.implementations.assistant_ops.user_activity', None),
    'manual_reader': ('core.tools.implementations.assistant_ops.manual_reader', None),
    'data_sync_status': ('core.tools.implementations.assistant_ops.data_sync_status', None),
    'paper_trading_status': ('core.tools.implementations.assistant_ops.paper_trading_status', None),
    'token_usage': ('core.tools.implementations.assistant_ops.token_usage', None),
    'running_tasks': ('core.tools.implementations.assistant_ops.running_tasks', None),
    'export_table_excel_tool': ('core.tools.implementations.assistant_ops.export_table_excel_tool', None),
}

__all__ = [
    'analysis_trigger',
    'report_query',
    'schedule_manager',
    'watchlist_manager',
    'favorite_manager',
    'position_analysis',
    'trade_review_ops',
    'risk_alerts',
    'topic_manager',
    'recent_search',
    'agent_executor',
    'stock_sync_ops',
    'web_search',
    'fetch_url',
    'set_stop_gain_alert',
    'list_stop_gain_alerts',
    'clear_stop_gain_alert',
    'user_state',
    'system_status',
    'user_activity',
    'manual_reader',
    'data_sync_status',
    'paper_trading_status',
    'token_usage',
    'running_tasks',
    'export_table_excel_tool',
]


def __getattr__(name):
    return resolve_export(name, _EXPORTS, globals())


def __dir__():
    return sorted(set(globals()) | set(_EXPORTS))
