"""
持仓分析服务
提供持仓管理和AI分析功能
"""

from app.services.portfolio.support import (
    asyncio,
    os,
    re,
    uuid,
    logging,
    datetime,
    timedelta,
    tz,
    Dict,
    Any,
    Optional,
    List,
    ObjectId,
    truncate_api_key,
    get_mongo_db,
    _format_user_friendly_error,
    _use_template_prompts,
    _use_stock_engine,
    _use_stock_engine,
    RealPosition,
    PositionSource,
    PortfolioAnalysisStatus,
    PositionAction,
    PositionSnapshot,
    PortfolioSnapshot,
    IndustryDistribution,
    AccountSnapshot,
    ConcentrationAnalysis,
    AIAnalysisResult,
    PortfolioAnalysisReport,
    PositionCreate,
    PositionUpdate,
    PositionResponse,
    PortfolioStatsResponse,
    PositionAnalysisRequest,
    PositionAnalysisReport,
    PositionAnalysisResult,
    PriceTarget,
    PositionAnalysisResponse,
    PositionRiskMetrics,
    PositionUserView,
    RealAccount,
    CapitalTransaction,
    CapitalTransactionType,
    AccountInitRequest,
    AccountTransactionRequest,
    AccountSettingsRequest,
    AccountSummary,
    PositionChangeType,
    PositionChange,
    PositionChangeResponse,
    PositionOperationType,
    PositionOperationRequest,
    now_tz,
    sanitize_position_analysis_payload,
    logger,
    ETF_CODE_PREFIXES,
    MARKET_CODE_TO_NAME,
    MARKET_NAME_TO_CODE,
    _is_cn_etf_symbol,
    convert_market_code_to_name,
    convert_market_name_to_code,
)

from app.services.portfolio.account import AccountMixin
from app.services.portfolio.position_changes import PositionChangeMixin
from app.services.portfolio.positions import PositionMixin
from app.services.portfolio.portfolio_stats import PortfolioStatsMixin
from app.services.portfolio.portfolio_report import PortfolioReportMixin
from app.services.portfolio.position_tasks import PositionTaskMixin
from app.services.portfolio.position_prompts import PositionPromptMixin
from app.services.portfolio.position_results import PositionResultMixin

class PortfolioService(AccountMixin, PositionChangeMixin, PositionMixin, PortfolioStatsMixin, PortfolioReportMixin, PositionTaskMixin, PositionPromptMixin, PositionResultMixin):

    def __init__(self):
        self.db = get_mongo_db()
        self.positions_collection = "real_positions"
        self.analysis_collection = "portfolio_analysis_reports"
        self.position_analysis_collection = "position_analysis_reports"
        self.paper_positions_collection = "paper_positions"
        self.accounts_collection = "real_accounts"
        self.transactions_collection = "capital_transactions"
        self.position_changes_collection = "position_changes"


# 服务实例
_portfolio_service: Optional[PortfolioService] = None


def get_portfolio_service() -> PortfolioService:
    """获取持仓分析服务实例"""
    global _portfolio_service
    if _portfolio_service is None:
        _portfolio_service = PortfolioService()
    return _portfolio_service

