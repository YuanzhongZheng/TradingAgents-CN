"""基本面分析工具

按需导入：导入包本身不会加载工具实现；全量发现请用 ToolRegistry.list_all()。
"""

from core.tools._lazy import resolve_export

_EXPORTS = {
    'get_stock_fundamentals_unified': ('.stock_fundamentals', 'get_stock_fundamentals_unified'),
    'get_income_analysis': ('.financial_tools', 'get_income_analysis'),
    'get_balance_sheet_analysis': ('.financial_tools', 'get_balance_sheet_analysis'),
    'get_cashflow_analysis': ('.financial_tools', 'get_cashflow_analysis'),
    'get_dividend_data': ('.financial_tools', 'get_dividend_data'),
    'get_main_business': ('.financial_tools', 'get_main_business'),
    'get_fundamental_factor_snapshot_tool': ('.factor_bundle_tools', 'get_fundamental_factor_snapshot_tool'),
    'get_growth_cashflow_factor_bundle_tool': ('.factor_bundle_tools', 'get_growth_cashflow_factor_bundle_tool'),
    'get_quality_factor_bundle_tool': ('.factor_bundle_tools', 'get_quality_factor_bundle_tool'),
    'get_value_factor_bundle_tool': ('.factor_bundle_tools', 'get_value_factor_bundle_tool'),
    'get_cashflow_quality_trend_tool': ('.standard_financial_tools', 'get_cashflow_quality_trend_tool'),
    'get_historical_financial_annual_series_tool': ('.standard_financial_tools', 'get_historical_financial_annual_series_tool'),
    'get_historical_valuation_percentile_tool': ('.standard_financial_tools', 'get_historical_valuation_percentile_tool'),
    'get_profitability_stability_metrics_tool': ('.standard_financial_tools', 'get_profitability_stability_metrics_tool'),
    'generate_valuation_excel_tool': ('.valuation_excel_tool', 'generate_valuation_excel_tool'),
    'get_bond_yield_calculation': ('.bond_tools', 'get_bond_yield_calculation'),
    'get_credit_spread_analysis': ('.bond_tools', 'get_credit_spread_analysis'),
    'get_bond_duration_convexity': ('.bond_tools', 'get_bond_duration_convexity'),
    'get_black_scholes_price': ('.option_tools', 'get_black_scholes_price'),
    'get_implied_volatility': ('.option_tools', 'get_implied_volatility'),
    'get_option_greeks': ('.option_tools', 'get_option_greeks'),
    'get_option_strategy_pnl': ('.option_tools', 'get_option_strategy_pnl'),
    'get_audit_opinion': ('.management_assessment', 'get_audit_opinion'),
    'get_disclosure_rating': ('.management_assessment', 'get_disclosure_rating'),
    'get_equity_incentive': ('.management_assessment', 'get_equity_incentive'),
    'get_management_penalties': ('.management_assessment', 'get_management_penalties'),
}

__all__ = [
    'get_stock_fundamentals_unified',
    'get_income_analysis',
    'get_balance_sheet_analysis',
    'get_cashflow_analysis',
    'get_dividend_data',
    'get_main_business',
    'get_value_factor_bundle_tool',
    'get_quality_factor_bundle_tool',
    'get_growth_cashflow_factor_bundle_tool',
    'get_fundamental_factor_snapshot_tool',
    'get_historical_financial_annual_series_tool',
    'get_profitability_stability_metrics_tool',
    'get_cashflow_quality_trend_tool',
    'get_historical_valuation_percentile_tool',
    'generate_valuation_excel_tool',
    'get_bond_yield_calculation',
    'get_credit_spread_analysis',
    'get_bond_duration_convexity',
    'get_black_scholes_price',
    'get_implied_volatility',
    'get_option_greeks',
    'get_option_strategy_pnl',
    'get_audit_opinion',
    'get_disclosure_rating',
    'get_equity_incentive',
    'get_management_penalties',
]


def __getattr__(name):
    return resolve_export(name, _EXPORTS, globals())


def __dir__():
    return sorted(set(globals()) | set(_EXPORTS))
