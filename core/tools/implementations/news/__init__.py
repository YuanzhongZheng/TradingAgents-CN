"""新闻数据工具

按需导入：导入包本身不会加载工具实现；全量发现请用 ToolRegistry.list_all()。
"""

from core.tools._lazy import resolve_export

_EXPORTS = {
    'get_stock_news_details_unified': ('.stock_news', 'get_stock_news_details_unified'),
    'get_stock_news_headlines_unified': ('.stock_news', 'get_stock_news_headlines_unified'),
    'get_stock_news_unified': ('.stock_news', 'get_stock_news_unified'),
}

__all__ = [
    'get_stock_news_unified',
    'get_stock_news_headlines_unified',
    'get_stock_news_details_unified',
]


def __getattr__(name):
    return resolve_export(name, _EXPORTS, globals())


def __dir__():
    return sorted(set(globals()) | set(_EXPORTS))
