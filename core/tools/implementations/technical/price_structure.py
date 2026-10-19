"""缠论与波浪理论的规则化结构计算。

默认分析周期是周线、日线、30 分钟和 5 分钟。
计算只使用 K 线高低点，不生成交易指令。
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Annotated, Dict, Iterable, List, Optional, Sequence, Tuple

from langchain_core.tools import tool

from core.tools.base import register_tool

logger = logging.getLogger(__name__)

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


@dataclass
class StructureBar:
    time: str
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


def parse_structure_periods(raw: Optional[str]) -> Tuple[str, ...]:
    if not raw or not str(raw).strip():
        return DEFAULT_STRUCTURE_PERIODS
    alias = {
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
    selected = []
    for part in str(raw).replace("，", ",").split(","):
        key = alias.get(part.strip())
        if key and key not in selected:
            selected.append(key)
    return tuple(selected) or DEFAULT_STRUCTURE_PERIODS


def bars_from_records(records: Iterable[dict]) -> List[StructureBar]:
    bars: List[StructureBar] = []
    for row in records or []:
        try:
            high = float(row.get("high"))
            low = float(row.get("low"))
            close = float(row.get("close"))
            open_price = float(row.get("open", close))
        except (TypeError, ValueError):
            continue
        if high < low:
            high, low = low, high
        volume = row.get("volume", row.get("vol", 0)) or 0
        try:
            volume = float(volume)
        except (TypeError, ValueError):
            volume = 0.0
        bars.append(StructureBar(
            time=str(row.get("time") or row.get("trade_date") or row.get("date") or ""),
            open=open_price,
            high=high,
            low=low,
            close=close,
            volume=volume,
        ))
    return bars


def _ema(values: Sequence[float], period: int) -> List[float]:
    if not values:
        return []
    alpha = 2 / (period + 1)
    output = [values[0]]
    for value in values[1:]:
        output.append(value * alpha + output[-1] * (1 - alpha))
    return output


def _macd_hist(closes: Sequence[float]) -> List[float]:
    fast = _ema(closes, 12)
    slow = _ema(closes, 26)
    diff = [left - right for left, right in zip(fast, slow)]
    signal = _ema(diff, 9)
    return [left - right for left, right in zip(diff, signal)]


def merge_chanlun_bars(bars: Sequence[StructureBar]) -> List[StructureBar]:
    """处理 K 线包含关系。向上取高高，向下取低低。"""
    if not bars:
        return []
    merged = [bars[0]]
    direction = 0
    for bar in bars[1:]:
        previous = merged[-1]
        included = (
            (bar.high <= previous.high and bar.low >= previous.low)
            or (previous.high <= bar.high and previous.low >= bar.low)
        )
        if not included:
            if bar.high >= previous.high and bar.low >= previous.low:
                direction = 1
            elif bar.high <= previous.high and bar.low <= previous.low:
                direction = -1
            merged.append(bar)
            continue
        if direction >= 0:
            high = max(previous.high, bar.high)
            low = max(previous.low, bar.low)
        else:
            high = min(previous.high, bar.high)
            low = min(previous.low, bar.low)
        merged[-1] = StructureBar(
            time=bar.time or previous.time,
            open=previous.open,
            high=high,
            low=low,
            close=bar.close,
            volume=previous.volume + bar.volume,
        )
    return merged


def chanlun_fractals(bars: Sequence[StructureBar]) -> List[Tuple[str, int, StructureBar]]:
    fractals = []
    for index in range(1, len(bars) - 1):
        left, middle, right = bars[index - 1], bars[index], bars[index + 1]
        if (
            middle.high > left.high and middle.high > right.high
            and middle.low > left.low and middle.low > right.low
        ):
            fractals.append(("top", index, middle))
        elif (
            middle.low < left.low and middle.low < right.low
            and middle.high < left.high and middle.high < right.high
        ):
            fractals.append(("bottom", index, middle))
    return fractals


def chanlun_bi(fractals: Sequence[Tuple[str, int, StructureBar]]) -> List[Tuple[str, int, StructureBar]]:
    """相邻分型至少间隔 4 根合并 K 线才构成一笔，同向分型保留更极端的一个。"""
    strokes: List[Tuple[str, int, StructureBar]] = []
    for kind, index, bar in fractals:
        if not strokes:
            strokes.append((kind, index, bar))
            continue
        last_kind, last_index, last_bar = strokes[-1]
        if kind == last_kind:
            if kind == "top" and bar.high >= last_bar.high:
                strokes[-1] = (kind, index, bar)
            elif kind == "bottom" and bar.low <= last_bar.low:
                strokes[-1] = (kind, index, bar)
            continue
        if index - last_index < 4:
            continue
        strokes.append((kind, index, bar))
    return strokes


def chanlun_zhongshu(strokes: Sequence[Tuple[str, int, StructureBar]]) -> List[Dict[str, float]]:
    segments = []
    for index in range(len(strokes) - 1):
        start = strokes[index][2]
        end = strokes[index + 1][2]
        segments.append((min(start.low, end.low), max(start.high, end.high)))
    centers = []
    cursor = 0
    while cursor + 2 < len(segments):
        lows = [segments[cursor + offset][0] for offset in range(3)]
        highs = [segments[cursor + offset][1] for offset in range(3)]
        lower = max(lows)
        upper = min(highs)
        if upper > lower:
            centers.append({"lower": lower, "upper": upper, "start_stroke": cursor + 1})
            cursor += 3
        else:
            cursor += 1
    return centers


def _stroke_moves(strokes: Sequence[Tuple[str, int, StructureBar]], hist: Sequence[float]):
    moves = []
    for index in range(len(strokes) - 1):
        start_kind, start_index, start = strokes[index]
        _end_kind, end_index, end = strokes[index + 1]
        direction = "down" if start_kind == "top" else "up"
        price_range = abs(start.high - end.low) if direction == "down" else abs(end.high - start.low)
        window = hist[start_index:end_index + 1]
        macd_area = sum(window) if window else 0.0
        moves.append({
            "direction": direction,
            "start": start.time,
            "end": end.time,
            "start_price": start.high if direction == "down" else start.low,
            "end_price": end.low if direction == "down" else end.high,
            "range": price_range,
            "macd_area": macd_area,
        })
    return moves


def analyze_chanlun_bars(bars: Sequence[StructureBar]) -> Dict[str, object]:
    if len(bars) < 20:
        return {"status": "insufficient", "bar_count": len(bars)}
    merged = merge_chanlun_bars(bars)
    fractals = chanlun_fractals(merged)
    strokes = chanlun_bi(fractals)
    centers = chanlun_zhongshu(strokes)
    hist = _macd_hist([bar.close for bar in merged])
    moves = _stroke_moves(strokes, hist)
    divergence = "未形成"
    same_direction = {}
    for move in moves:
        same_direction.setdefault(move["direction"], []).append(move)
    for direction, label in (("down", "下跌笔疑似背驰"), ("up", "上涨笔疑似背驰")):
        group = same_direction.get(direction) or []
        if len(group) < 2:
            continue
        previous, latest = group[-2], group[-1]
        if (
            latest["range"] < previous["range"]
            and abs(latest["macd_area"]) < abs(previous["macd_area"])
            and previous["range"] > 0
        ):
            divergence = label
            break
    return {
        "status": "ok",
        "bar_count": len(bars),
        "merged_count": len(merged),
        "strokes": strokes,
        "moves": moves[-6:],
        "centers": centers[-2:],
        "divergence": divergence,
        "latest_time": bars[-1].time,
        "latest_close": bars[-1].close,
    }


def zigzag_pivots(bars: Sequence[StructureBar], threshold: float) -> List[Tuple[str, StructureBar]]:
    if len(bars) < 3 or threshold <= 0:
        return []
    pivots: List[Tuple[str, StructureBar]] = []
    mode: Optional[str] = None
    extreme = 0
    for index in range(1, len(bars)):
        if mode is None:
            base = bars[0].close or bars[0].high
            if base <= 0:
                continue
            change = (bars[index].close - base) / base
            if abs(change) < threshold:
                continue
            mode = "up" if change > 0 else "down"
            pivots.append(("low" if mode == "up" else "high", bars[0]))
            extreme = index
            continue
        if mode == "up":
            if bars[index].high >= bars[extreme].high:
                extreme = index
                continue
            if bars[extreme].high > 0 and (bars[extreme].high - bars[index].low) / bars[extreme].high >= threshold:
                pivots.append(("high", bars[extreme]))
                mode = "down"
                extreme = index
        else:
            if bars[index].low <= bars[extreme].low:
                extreme = index
                continue
            if bars[extreme].low > 0 and (bars[index].high - bars[extreme].low) / bars[extreme].low >= threshold:
                pivots.append(("low", bars[extreme]))
                mode = "up"
                extreme = index
    if mode == "up":
        pivots.append(("high", bars[extreme]))
    elif mode == "down":
        pivots.append(("low", bars[extreme]))
    return pivots


def _price_of(pivot: Tuple[str, StructureBar]) -> float:
    kind, bar = pivot
    return bar.high if kind == "high" else bar.low


def _label_elliott(pivots: Sequence[Tuple[str, StructureBar]]) -> Dict[str, object]:
    if len(pivots) < 4:
        return {"pattern": "样本不足", "waves": []}
    points = pivots[-6:] if len(pivots) >= 6 else pivots[-4:]
    prices = [_price_of(point) for point in points]
    waves = []
    for index in range(len(points) - 1):
        start_kind, start_bar = points[index]
        _end_kind, end_bar = points[index + 1]
        waves.append({
            "start": start_bar.time,
            "end": end_bar.time,
            "start_price": _price_of(points[index]),
            "end_price": _price_of(points[index + 1]),
            "direction": "up" if start_kind == "low" else "down",
        })
    pattern = "调整结构"
    if len(prices) >= 6 and points[0][0] == "low":
        wave1 = prices[1] - prices[0]
        wave2_ok = prices[2] > prices[0]
        wave3 = prices[3] - prices[2]
        wave5 = prices[5] - prices[4]
        wave4_ok = prices[4] > prices[1]
        wave3_not_shortest = wave3 >= wave1 and wave3 >= wave5 and wave3 > 0
        if wave2_ok and wave4_ok and wave3_not_shortest and prices[3] > prices[1]:
            pattern = "上升推动五浪"
            if prices[5] <= prices[3]:
                pattern = "上升推动五浪，第5浪未创新高"
    elif len(prices) >= 6 and points[0][0] == "high":
        wave1 = prices[0] - prices[1]
        wave2_ok = prices[2] < prices[0]
        wave3 = prices[2] - prices[3]
        wave5 = prices[4] - prices[5]
        wave4_ok = prices[4] < prices[1]
        wave3_not_shortest = wave3 >= wave1 and wave3 >= wave5 and wave3 > 0
        if wave2_ok and wave4_ok and wave3_not_shortest and prices[3] < prices[1]:
            pattern = "下降推动五浪"
            if prices[5] >= prices[3]:
                pattern = "下降推动五浪，第5浪未创新低"
    elif len(prices) >= 4:
        pattern = "ABC 调整结构"
    return {"pattern": pattern, "waves": waves[-5:]}


def analyze_elliott_bars(bars: Sequence[StructureBar], threshold: float) -> Dict[str, object]:
    if len(bars) < 20:
        return {"status": "insufficient", "bar_count": len(bars)}
    pivots = zigzag_pivots(bars, threshold)
    labeled = _label_elliott(pivots)
    return {
        "status": "ok",
        "bar_count": len(bars),
        "threshold": threshold,
        "pivots": pivots[-8:],
        "pattern": labeled["pattern"],
        "waves": labeled["waves"],
        "latest_time": bars[-1].time,
        "latest_close": bars[-1].close,
    }


def _fmt(value: float) -> str:
    return f"{value:.2f}"


def render_chanlun(period: str, result: Dict[str, object], source: str = "") -> str:
    label = PERIOD_LABELS.get(period, period)
    if result.get("status") != "ok":
        return f"### {label}\n样本不足（{result.get('bar_count', 0)} 根），不划分笔和中枢。"
    lines = [
        f"### {label}",
        f"- K线 {result['bar_count']} 根，包含处理后 {result['merged_count']} 根，最新收盘 {_fmt(result['latest_close'])}（{result['latest_time']}）",
    ]
    if source:
        lines.append(f"- 数据源: {source}")
    moves = result.get("moves") or []
    if moves:
        lines.append("- 最近的笔:")
        for move in moves:
            lines.append(
                f"  - {move['direction']} {move['start']} {_fmt(move['start_price'])} -> "
                f"{move['end']} {_fmt(move['end_price'])}"
            )
    else:
        lines.append("- 最近的笔: 未形成有效笔")
    centers = result.get("centers") or []
    if centers:
        last = centers[-1]
        lines.append(f"- 最近中枢: {_fmt(last['lower'])} - {_fmt(last['upper'])}")
    else:
        lines.append("- 最近中枢: 未形成三段重叠")
    lines.append(f"- 背驰观察: {result['divergence']}")
    return "\n".join(lines)


def render_elliott(period: str, result: Dict[str, object], source: str = "") -> str:
    label = PERIOD_LABELS.get(period, period)
    if result.get("status") != "ok":
        return f"### {label}\n样本不足（{result.get('bar_count', 0)} 根），不划分浪。"
    lines = [
        f"### {label}",
        f"- K线 {result['bar_count']} 根，摆动阈值 {result['threshold']:.0%}，最新收盘 {_fmt(result['latest_close'])}（{result['latest_time']}）",
        f"- 结构: {result['pattern']}",
    ]
    if source:
        lines.append(f"- 数据源: {source}")
    pivots = result.get("pivots") or []
    if pivots:
        text = " -> ".join(f"{kind} {_fmt(_price_of((kind, bar)))}@{bar.time}" for kind, bar in pivots)
        lines.append(f"- 拐点: {text}")
    waves = result.get("waves") or []
    if waves:
        lines.append("- 最近浪段:")
        for index, wave in enumerate(waves, start=1):
            lines.append(
                f"  - {index}. {wave['direction']} {wave['start']} {_fmt(wave['start_price'])} -> "
                f"{wave['end']} {_fmt(wave['end_price'])}"
            )
    return "\n".join(lines)


def load_structure_bars(symbol: str, period: str, end_date: Optional[str] = None) -> Tuple[List[StructureBar], str]:
    from app.services.data_sources.manager import ANALYSIS_BAR_ADJUST, DataSourceManager

    manager = DataSourceManager()
    use_end = end_date if period in ("day", "week") else None
    items, source = manager.get_kline_with_fallback(
        code=symbol,
        period=period,
        limit=PERIOD_LIMITS.get(period, 120),
        adj=ANALYSIS_BAR_ADJUST,
        end_date=use_end,
    )
    return bars_from_records(items or []), source or ""


def _load_period(symbol: str, period: str, end_date: Optional[str]) -> Tuple[str, List[StructureBar], str, str]:
    try:
        bars, source = load_structure_bars(symbol, period, end_date)
        return period, bars, source, ""
    except Exception as exc:
        logger.warning("结构分析读取 %s %s 失败: %s", symbol, period, exc)
        return period, [], "", str(exc)


def analyze_price_structure(
    symbol: str,
    end_date: Optional[str] = None,
    periods: Optional[Sequence[str]] = None,
) -> Dict[str, Dict[str, object]]:
    selected = tuple(periods or DEFAULT_STRUCTURE_PERIODS)
    loaded = []
    with ThreadPoolExecutor(max_workers=min(4, len(selected) or 1)) as pool:
        loaded = list(pool.map(lambda period: _load_period(symbol, period, end_date), selected))
    output = {}
    for period, bars, source, error in loaded:
        output[period] = {
            "source": source,
            "error": error,
            "chanlun": analyze_chanlun_bars(bars),
            "elliott": analyze_elliott_bars(bars, PERIOD_SWING.get(period, 0.05)),
        }
    return output


def render_price_structure(symbol: str, results: Dict[str, Dict[str, object]]) -> str:
    period_text = "、".join(PERIOD_LABELS[item] for item in results)
    chanlun_parts = ["## 缠论结构", f"默认周期：{period_text}。下面只记录已经算出来的笔、中枢和背驰观察。"]
    elliott_parts = ["## 波浪结构", f"默认周期：{period_text}。下面只记录摆动拐点和对最近一段的浪型归类。"]
    for period, payload in results.items():
        if payload.get("error"):
            label = PERIOD_LABELS.get(period, period)
            chanlun_parts.append(f"### {label}\n读取失败: {payload['error']}")
            elliott_parts.append(f"### {label}\n读取失败: {payload['error']}")
            continue
        source = str(payload.get("source") or "")
        chanlun_parts.append(render_chanlun(period, payload["chanlun"], source))
        elliott_parts.append(render_elliott(period, payload["elliott"], source))
    return f"# {symbol} 价格结构\n\n" + "\n\n".join(chanlun_parts + [""] + elliott_parts)


def build_price_structure_context(symbol: str, end_date: Optional[str] = None) -> str:
    results = analyze_price_structure(symbol, end_date=end_date)
    return render_price_structure(symbol, results)


def _tool_report(kind: str, ticker: str, analysis_date: str, periods: str) -> str:
    selected = parse_structure_periods(periods)
    results = analyze_price_structure(ticker, end_date=analysis_date or None, periods=selected)
    if kind == "chanlun":
        parts = [f"# {ticker} 缠论结构", "默认周期：周线、日线、30分钟、5分钟。"]
        for period, payload in results.items():
            if payload.get("error"):
                parts.append(f"### {PERIOD_LABELS.get(period, period)}\n读取失败: {payload['error']}")
            else:
                parts.append(render_chanlun(period, payload["chanlun"], str(payload.get("source") or "")))
        return "\n\n".join(parts)
    parts = [f"# {ticker} 波浪结构", "默认周期：周线、日线、30分钟、5分钟。"]
    for period, payload in results.items():
        if payload.get("error"):
            parts.append(f"### {PERIOD_LABELS.get(period, period)}\n读取失败: {payload['error']}")
        else:
            parts.append(render_elliott(period, payload["elliott"], str(payload.get("source") or "")))
    return "\n\n".join(parts)


@tool
@register_tool(
    tool_id="analyze_chanlun",
    name="缠论结构分析",
    description="按缠论规则划分笔、中枢和背驰观察。默认周期为周线、日线、30分钟、5分钟。",
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
    description="按摆动拐点划分波浪结构，检查推动浪的基本规则。默认周期为周线、日线、30分钟、5分钟。",
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
