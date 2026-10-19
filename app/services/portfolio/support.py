"""
持仓分析服务
提供持仓管理和AI分析功能
"""

import asyncio
import os
import re
import uuid
import logging
from datetime import datetime, timedelta, timezone as tz
from typing import Dict, Any, Optional, List
from bson import ObjectId
from app.utils.api_key_utils import truncate_api_key

from app.core.database import get_mongo_db


# ==================== 工具函数 ====================
def _format_user_friendly_error(error_msg: str) -> str:
    """将技术性/英文错误消息转换为用户可理解的友好中文提示。

    针对常见的限流、网络、超时、余额不足等情况做专门处理，
    避免把英文报错直接展示给用户。
    """
    if not error_msg:
        return "分析过程中出现错误，请稍后重试"

    msg = error_msg.lower()

    # 限流类（429 / RATE_LIMIT）
    if any(key in msg for key in (
        "429", "rate_limit", "rate limit", "tokens limit",
        "rate_limited", "访问限制", "限流", "quota exceeded"
    )):
        return "当前使用AI分析的人数较多，服务繁忙，请稍后再试。（提示：避免短时间内连续发起多次分析）"

    # 网络/连接类
    if any(key in msg for key in (
        "network error", "connection error", "connection refused",
        "failed to fetch", "连接失败", "网络错误",
    )):
        return "网络连接失败，请检查网络状态后重试。"

    # 超时类
    if any(key in msg for key in ("timeout", "timed out", "超时")):
        return "请求超时，请稍后重试。若频繁出现，请联系管理员。"

    # 认证/权限类
    if any(key in msg for key in ("401", "403", "unauthorized", "forbidden", "无权限", "未授权")):
        return "登录已过期，请刷新页面重新登录后再试。"

    # 余额/额度不足
    if any(key in msg for key in (
        "insufficient balance", "余额不足", "额度不足", "out of quota",
    )):
        return "AI分析额度不足，请联系管理员补充额度后再试。"

    # 服务端错误
    if any(key in msg for key in ("server error", "500", "502", "503", "internal error")):
        return "服务器暂时不可用，请稍后重试。"

    # 如果是中文且相对简短，直接返回
    import re as _re
    if _re.search(r"[\u4e00-\u9fa5]", error_msg) and len(error_msg) <= 80:
        return error_msg

    # 默认兜底
    return "分析过程中出现错误，请稍后重试"


# ==================== 特性开关 ====================
# 是否使用模板系统生成提示词（默认关闭，保持向后兼容）
# 注意：使用函数动态读取，确保 .env 文件已加载
def _use_template_prompts() -> bool:
    """动态读取 USE_TEMPLATE_PROMPTS 环境变量"""
    return os.getenv("USE_TEMPLATE_PROMPTS", "false").lower() == "true"

# 是否使用新版股票分析引擎（默认关闭，保持向后兼容）
def _use_stock_engine() -> bool:
    """动态读取 USE_STOCK_ENGINE 环境变量"""
    return os.getenv("USE_STOCK_ENGINE", "false").lower() == "true"

# 是否使用新版分析引擎（包括工作流引擎）
def _use_stock_engine() -> bool:
    """动态读取 USE_STOCK_ENGINE 环境变量"""
    return os.getenv("USE_STOCK_ENGINE", "false").lower() == "true"

from app.models.portfolio import (
    RealPosition, PositionSource, PortfolioAnalysisStatus, PositionAction,
    PositionSnapshot, PortfolioSnapshot, IndustryDistribution, AccountSnapshot,
    ConcentrationAnalysis, AIAnalysisResult, PortfolioAnalysisReport,
    PositionCreate, PositionUpdate, PositionResponse, PortfolioStatsResponse,
    PositionAnalysisRequest, PositionAnalysisReport, PositionAnalysisResult,
    PriceTarget, PositionAnalysisResponse, PositionRiskMetrics,
    PositionUserView,
    RealAccount, CapitalTransaction, CapitalTransactionType,
    AccountInitRequest, AccountTransactionRequest, AccountSettingsRequest, AccountSummary,
    PositionChangeType, PositionChange, PositionChangeResponse,
    PositionOperationType, PositionOperationRequest
)
from app.utils.timezone import now_tz
from app.utils.compliance import sanitize_position_analysis_payload

logger = logging.getLogger("app.services.portfolio_service")

ETF_CODE_PREFIXES = ("50", "51", "52", "56", "58", "15", "16", "18")

# 市场代码映射：前端/数据库使用的代码 -> 单股分析服务使用的中文名称
MARKET_CODE_TO_NAME = {
    "CN": "A股",
    "HK": "港股",
    "US": "美股"
}

# 市场名称映射：中文名称 -> 代码
MARKET_NAME_TO_CODE = {
    "A股": "CN",
    "港股": "HK",
    "美股": "US"
}


def _is_cn_etf_symbol(code: str) -> bool:
    code_str = str(code or "").strip().upper().zfill(6)
    return len(code_str) == 6 and code_str.isdigit() and code_str.startswith(ETF_CODE_PREFIXES)


def convert_market_code_to_name(market_code: str, stock_code: Optional[str] = None) -> str:
    """将市场代码转换为中文名称（用于单股分析服务）"""
    if str(market_code).upper() == "ETF":
        return "ETF"
    if str(market_code).upper() == "CN" and stock_code and _is_cn_etf_symbol(stock_code):
        return "ETF"
    return MARKET_CODE_TO_NAME.get(market_code, market_code)


def convert_market_name_to_code(market_name: str) -> str:
    """将市场中文名称转换为代码（用于数据库存储）"""
    return MARKET_NAME_TO_CODE.get(market_name, market_name)


