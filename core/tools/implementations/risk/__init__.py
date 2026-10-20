"""风险评分工具模块

按需导入：导入包本身不会加载工具实现；全量发现请用 ToolRegistry.list_all()。
"""

from core.tools._lazy import resolve_export

_EXPORTS = {
    'get_altman_zscore': ('.altman_zscore', 'get_altman_zscore'),
    'get_beneish_mscore': ('.beneish_mscore', 'get_beneish_mscore'),
    'get_earnings_quality_score': ('.earnings_quality', 'get_earnings_quality_score'),
    'get_piotroski_fscore': ('.piotroski_fscore', 'get_piotroski_fscore'),
}

__all__ = [
    'get_altman_zscore',
    'get_beneish_mscore',
    'get_earnings_quality_score',
    'get_piotroski_fscore',
]


def __getattr__(name):
    return resolve_export(name, _EXPORTS, globals())


def __dir__():
    return sorted(set(globals()) | set(_EXPORTS))
