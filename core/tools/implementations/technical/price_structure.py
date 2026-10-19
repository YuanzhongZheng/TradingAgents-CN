"""缠论与波浪理论的多周期价格结构。

默认分析周期是周线、日线、30 分钟和 5 分钟。各周期分开计算，结果是候选结构，
不生成交易指令。算法在 chanlun.py 和 elliott_wave.py，这里负责取数、缓存、
周期对照和工具入口。
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Annotated, Dict, List, Optional, Sequence, Tuple

from langchain_core.tools import tool

from core.tools.base import register_tool

from .chanlun import analyze_chanlun_bars, render_chanlun
from .elliott_wave import analyze_elliott_bars, render_elliott
from .structure_bars import StructureBar, bars_from_records, direction_label

logger = logging.getLogger(__name__)
_STRUCTURE_CACHE_TTL_SECONDS = 180
_STRUCTURE_CACHE: Dict[tuple, tuple] = {}
_STRUCTURE_LOCKS: Dict[tuple, threading.Lock] = {}
_STRUCTURE_CACHE_GUARD = threading.Lock()

DEFAULT_STRUCTURE_PERIODS: Tuple[str, ...] = ("week", "day", "30m", "5m")
PERIOD_LABELS = {
    "week": "周线",
    "day": "日线",
    "30m": "30分钟",
    "5m": "5分钟",
}
PERIOD_LIMITS = {
    "week": 120,
    "day": 250,
    "30m": 240,
    "5m": 480,
}
PERIOD_SWING = {
    "week": 0.08,
    "day": 0.05,
    "30m": 0.02,
    "5m": 0.01,
}
_PERIOD_ALIASES = {
    "weekly": "week",
    "week": "week",
    "w": "week",
    "周": "week",
    "周线": "week",
    "daily": "day",
    "day": "day",
    "d": "day",
    "日": "day",
    "日线": "day",
    "30": "30m",
    "30m": "30m",
    "30min": "30m",
    "30分钟": "30m",
    "5": "5m",
    "5m": "5m",
    "5min": "5m",
    "5分钟": "5m",
}


def parse_structure_periods(raw: Optional[str]) -> Tuple[str, ...]:
    if not raw or not str(raw).strip():
        return DEFAULT_STRUCTURE_PERIODS
    selected = []
    for part in str(raw).replace("，", ",").split(","):
        key = _PERIOD_ALIASES.get(part.strip())
        if key and key not in selected:
            selected.append(key)
    return tuple(selected) or DEFAULT_STRUCTURE_PERIODS


def _period_label(period: str) -> str:
    return PERIOD_LABELS.get(period, period)


def _bar_day(bar: StructureBar) -> str:
    digits = "".join(ch for ch in (bar.time or "") if ch.isdigit())
    if len(digits) < 8:
        return ""
    return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"


def structure_request_window(period: str, end_date: Optional[str]) -> Tuple[Optional[str], Optional[str], int]:
    """按根数上限回推开始日期，避免只传截止日期时从 1990 年取数。"""
    limit = PERIOD_LIMITS.get(period, 120)
    if not end_date:
        return None, None, limit
    digits = "".join(ch for ch in str(end_date) if ch.isdigit())
    if len(digits) < 8:
        return None, None, limit
    end = datetime(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
    span_days = {
        "week": limit * 7 + 21,
        "day": int(limit * 1.8) + 15,
        "30m": int(limit / 8 * 2) + 10,
        "5m": int(limit / 48 * 2) + 5,
    }.get(period, limit * 2)
    start = end - timedelta(days=span_days)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"), limit


def clip_structure_bars(
    bars: Sequence[StructureBar],
    start_date: Optional[str],
    end_date: Optional[str],
    limit: int,
) -> Tuple[List[StructureBar], str]:
    selected = list(bars)
    if end_date:
        selected = [
            bar for bar in selected
            if _bar_day(bar) and (not start_date or _bar_day(bar) >= start_date) and _bar_day(bar) <= end_date
        ]
        if not selected:
            return [], "unavailable"
    if limit > 0:
        selected = selected[-limit:]
    return [replace(bar, start_index=index, end_index=index) for index, bar in enumerate(selected)], "ok"


def load_structure_bars(symbol: str, period: str, end_date: Optional[str] = None) -> Tuple[List[StructureBar], str, str]:
    from app.services.data_sources.manager import ANALYSIS_BAR_ADJUST, DataSourceManager

    manager = DataSourceManager()
    start_date, bounded_end, limit = structure_request_window(period, end_date)
    items, source = manager.get_kline_with_fallback(
        code=symbol,
        period=period,
        limit=limit,
        adj=ANALYSIS_BAR_ADJUST,
        start_date=start_date,
        end_date=bounded_end,
    )
    bars, availability = clip_structure_bars(bars_from_records(items or []), start_date, bounded_end, limit)
    return bars, source or "", availability


def _load_period(symbol: str, period: str, end_date: Optional[str]) -> Tuple[str, List[StructureBar], str, str, str]:
    try:
        bars, source, availability = load_structure_bars(symbol, period, end_date)
        return period, bars, source, "", availability
    except Exception as exc:
        logger.warning("结构分析读取 %s %s 失败: %s", symbol, period, exc)
        return period, [], "", str(exc), "error"


def _unavailable_result() -> Dict[str, object]:
    return {"status": "unavailable", "bar_count": 0}


def _structure_cache_lock(key: tuple) -> threading.Lock:
    with _STRUCTURE_CACHE_GUARD:
        lock = _STRUCTURE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _STRUCTURE_LOCKS[key] = lock
        return lock


def _cached_structure(key: tuple):
    item = _STRUCTURE_CACHE.get(key)
    if item is None:
        return None
    stored_at, value = item
    if time.monotonic() - stored_at > _STRUCTURE_CACHE_TTL_SECONDS:
        _STRUCTURE_CACHE.pop(key, None)
        return None
    return value


def _store_structure(key: tuple, value) -> None:
    if len(_STRUCTURE_CACHE) > 64:
        oldest = min(_STRUCTURE_CACHE, key=lambda item: _STRUCTURE_CACHE[item][0])
        _STRUCTURE_CACHE.pop(oldest, None)
    _STRUCTURE_CACHE[key] = (time.monotonic(), value)


def clear_structure_cache() -> None:
    with _STRUCTURE_CACHE_GUARD:
        _STRUCTURE_CACHE.clear()


def _analyze_price_structure_uncached(
    symbol: str,
    end_date: Optional[str],
    selected: Sequence[str],
) -> Dict[str, Dict[str, object]]:
    with ThreadPoolExecutor(max_workers=min(4, len(selected) or 1)) as pool:
        loaded = list(pool.map(lambda period: _load_period(symbol, period, end_date), selected))
    output = {}
    for period, bars, source, error, availability in loaded:
        if availability == "unavailable":
            output[period] = {
                "source": source,
                "error": error,
                "chanlun": _unavailable_result(),
                "elliott": _unavailable_result(),
            }
            continue
        output[period] = {
            "source": source,
            "error": error,
            "chanlun": analyze_chanlun_bars(bars),
            "elliott": analyze_elliott_bars(bars, PERIOD_SWING.get(period, 0.05)),
        }
    return output


def analyze_price_structure(
    symbol: str,
    end_date: Optional[str] = None,
    periods: Optional[Sequence[str]] = None,
) -> Dict[str, Dict[str, object]]:
    selected = tuple(periods or DEFAULT_STRUCTURE_PERIODS)
    key = (symbol, end_date or "", selected)
    cached = _cached_structure(key)
    if cached is not None:
        return cached
    with _structure_cache_lock(key):
        cached = _cached_structure(key)
        if cached is not None:
            return cached
        result = _analyze_price_structure_uncached(symbol, end_date, selected)
        _store_structure(key, result)
        return result


def render_cross_period(results: Dict[str, Dict[str, object]]) -> str:
    """各周期最近一笔方向和波浪候选的并排对照，不推导级别之间的包含关系。"""
    lines = [
        "## 周期对照",
        "这里只并排比较各周期最近一笔的方向和波浪候选，不是级别联立，也不推导次级别走势是否完成。",
    ]
    directions: List[Tuple[str, str]] = []
    for period, payload in results.items():
        label = _period_label(period)
        chan = payload.get("chanlun") or {}
        wave = payload.get("elliott") or {}
        if payload.get("error") or chan.get("status") != "ok":
            lines.append(f"- {label}: 没有可用结构")
            continue
        direction = chan.get("last_direction")
        stroke_text = f"最近一笔{direction_label(direction)}" if direction else "尚未形成完整的笔"
        wave_text = wave.get("core_pattern") or "样本不足"
        if wave.get("terminal_status") == "candidate":
            wave_text += "（末端未确认）"
        lines.append(f"- {label}: {stroke_text}；{chan.get('center_position', '')}；波浪 {wave_text}")
        if direction:
            directions.append((label, direction))
    if len(directions) >= 2:
        pairs = []
        for (left_label, left), (right_label, right) in zip(directions, directions[1:]):
            relation = "同向" if left == right else "反向"
            pairs.append(f"{left_label}与{right_label}{relation}")
        lines.append("- 相邻周期最近一笔: " + "，".join(pairs))
    return "\n".join(lines)


def _period_section(kind: str, period: str, payload: Dict[str, object]) -> str:
    label = _period_label(period)
    if payload.get("error"):
        return f"### {label}\n读取失败: {payload['error']}"
    source = str(payload.get("source") or "")
    if kind == "chanlun":
        return render_chanlun(label, payload["chanlun"], source)
    return render_elliott(label, payload["elliott"], source)


def render_price_structure(symbol: str, results: Dict[str, Dict[str, object]]) -> str:
    period_text = "、".join(_period_label(item) for item in results)
    chanlun_parts = [
        "## 缠论结构",
        (
            f"默认周期：{period_text}。四个周期分开计算，没有跨周期对应，不能称为多级别联合分析。"
            "下面是笔和三笔重叠近似，背驰只到候选，不是已确认的缠论事实。"
        ),
    ]
    elliott_parts = [
        "## 波浪结构",
        (
            f"默认周期：{period_text}。四个周期分开计算，没有跨周期对应。"
            "下面只是已确认摆动拐点上的候选形态，没有验证内部子浪。"
        ),
    ]
    for period, payload in results.items():
        chanlun_parts.append(_period_section("chanlun", period, payload))
        elliott_parts.append(_period_section("elliott", period, payload))
    sections = ["\n\n".join(chanlun_parts), "\n\n".join(elliott_parts), render_cross_period(results)]
    return f"# {symbol} 价格结构\n\n" + "\n\n".join(sections)


def build_price_structure_context(symbol: str, end_date: Optional[str] = None) -> str:
    results = analyze_price_structure(symbol, end_date=end_date)
    return render_price_structure(symbol, results)


def _tool_report(kind: str, ticker: str, analysis_date: str, periods: str) -> str:
    selected = parse_structure_periods(periods)
    results = analyze_price_structure(ticker, end_date=analysis_date or None, periods=selected)
    period_text = "、".join(_period_label(item) for item in results)
    title = "缠论结构" if kind == "chanlun" else "波浪结构"
    parts = [f"# {ticker} {title}", f"周期：{period_text}。各周期分开计算，结果是候选结构。"]
    parts.extend(_period_section(kind, period, payload) for period, payload in results.items())
    parts.append(render_cross_period(results))
    return "\n\n".join(parts)


@tool
@register_tool(
    tool_id="analyze_chanlun",
    name="缠论结构分析",
    description=(
        "按笔和三笔重叠近似整理价格结构，在重叠区间背景下比较进入段与离开段的力度，"
        "背驰只给出候选。默认周期为周线、日线、30分钟、5分钟。"
    ),
    category="technical",
    is_online=True,
    auto_register=True,
    capability_tags=["chanlun", "price_structure", "technical_analysis"],
    when_to_use="分析个股价格结构、笔、中枢、背驰，或用户明确要求缠论时使用。",
)
def analyze_chanlun(
    ticker: Annotated[str, "股票代码，如 600519"],
    analysis_date: Annotated[str, "分析日期 YYYY-MM-DD，可留空表示截至最新"] = "",
    periods: Annotated[str, "周期，逗号分隔。留空则使用周线、日线、30分钟、5分钟"] = "",
) -> str:
    """按缠论规则分析周线、日线、30分钟和5分钟结构。"""
    try:
        return _tool_report("chanlun", ticker, analysis_date, periods)
    except Exception as exc:
        logger.exception("缠论分析失败")
        return f"缠论分析失败: {exc}"


@tool
@register_tool(
    tool_id="analyze_elliott_wave",
    name="波浪结构分析",
    description=(
        "按已确认摆动拐点给出波浪候选，逐条检查推动浪规则并列出常见比例，"
        "单独标出未确认的末端拐点。不验证内部子浪。默认周期为周线、日线、30分钟、5分钟。"
    ),
    category="technical",
    is_online=True,
    auto_register=True,
    capability_tags=["elliott_wave", "price_structure", "technical_analysis"],
    when_to_use="分析个股波浪结构，或用户明确要求波浪理论时使用。",
)
def analyze_elliott_wave(
    ticker: Annotated[str, "股票代码，如 600519"],
    analysis_date: Annotated[str, "分析日期 YYYY-MM-DD，可留空表示截至最新"] = "",
    periods: Annotated[str, "周期，逗号分隔。留空则使用周线、日线、30分钟、5分钟"] = "",
) -> str:
    """按波浪理论规则分析周线、日线、30分钟和5分钟结构。"""
    try:
        return _tool_report("elliott", ticker, analysis_date, periods)
    except Exception as exc:
        logger.exception("波浪分析失败")
        return f"波浪分析失败: {exc}"
