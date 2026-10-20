"""市场数据工具

按需导入：导入包本身不会加载工具实现；全量发现请用 ToolRegistry.list_all()。
"""

from core.tools._lazy import resolve_export

_EXPORTS = {
    'get_stock_market_data_unified': ('.stock_market_data', 'get_stock_market_data_unified'),
    'get_china_market_overview': ('.china_market_overview', 'get_china_market_overview'),
    'get_technical_indicators': ('.technical_indicators', 'get_technical_indicators'),
    'get_technical_factor_bundle_tool': ('.technical_factor_bundle_tool', 'get_technical_factor_bundle_tool'),
    'get_market_overview_qmt': ('.qmt_market_tools', 'get_market_overview_qmt'),
    'get_sector_analysis_qmt': ('.qmt_market_tools', 'get_sector_analysis_qmt'),
    'get_auto_brand_monthly_sales': ('.dongchedi_sales_rank', 'get_auto_brand_monthly_sales'),
    'get_index_data': ('.index_market_tools', 'get_index_data'),
    'get_market_breadth': ('.index_market_tools', 'get_market_breadth'),
    'get_market_environment': ('.index_market_tools', 'get_market_environment'),
    'get_market_overview': ('.index_market_tools', 'get_market_overview'),
    'identify_market_cycle': ('.index_market_tools', 'identify_market_cycle'),
    'get_north_flow': ('.index_market_tools', 'get_north_flow'),
    'get_margin_trading': ('.index_market_tools', 'get_margin_trading'),
    'get_limit_stats': ('.index_market_tools', 'get_limit_stats'),
    'get_index_technical': ('.index_market_tools', 'get_index_technical'),
    'get_sector_data': ('.sector_market_tools', 'get_sector_data'),
    'get_fund_flow_data': ('.sector_market_tools', 'get_fund_flow_data'),
    'get_peer_comparison': ('.sector_market_tools', 'get_peer_comparison'),
    'analyze_sector': ('.sector_market_tools', 'analyze_sector'),
}

__all__ = [
    'get_stock_market_data_unified',
    'get_china_market_overview',
    'get_technical_indicators',
    'get_technical_factor_bundle_tool',
    'get_index_data',
    'get_market_breadth',
    'get_market_environment',
    'get_market_overview',
    'identify_market_cycle',
    'get_north_flow',
    'get_margin_trading',
    'get_limit_stats',
    'get_index_technical',
    'get_market_overview_qmt',
    'get_auto_brand_monthly_sales',
    'get_sector_data',
    'get_fund_flow_data',
    'get_peer_comparison',
    'analyze_sector',
    'get_sector_analysis_qmt',
]


def __getattr__(name):
    return resolve_export(name, _EXPORTS, globals())


def __dir__():
    return sorted(set(globals()) | set(_EXPORTS))
