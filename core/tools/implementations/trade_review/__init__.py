"""交易复盘工具集

提供交易复盘所需的数据获取和处理工具

按需导入：导入包本身不会加载工具实现；全量发现请用 ToolRegistry.list_all()。
"""

from core.tools._lazy import resolve_export

_EXPORTS = {
    'get_trade_records': ('.trade_records', 'get_trade_records'),
    'build_trade_info': ('.trade_info', 'build_trade_info'),
    'get_account_info': ('.account_info', 'get_account_info'),
    'get_market_snapshot_for_review': ('.market_snapshot', 'get_market_snapshot_for_review'),
}

__all__ = [
    'get_trade_records',
    'build_trade_info',
    'get_account_info',
    'get_market_snapshot_for_review',
]


def __getattr__(name):
    return resolve_export(name, _EXPORTS, globals())


def __dir__():
    return sorted(set(globals()) | set(_EXPORTS))
