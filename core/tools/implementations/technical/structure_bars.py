"""缠论和波浪分析共用的 K 线结构与动能计算。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Sequence


@dataclass
class StructureBar:
    time: str
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    start_index: int = -1
    end_index: int = -1


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
        position = len(bars)
        bars.append(StructureBar(
            time=str(row.get("time") or row.get("trade_date") or row.get("date") or ""),
            open=open_price,
            high=high,
            low=low,
            close=close,
            volume=volume,
            start_index=position,
            end_index=position,
        ))
    return bars


def ema(values: Sequence[float], period: int) -> List[float]:
    if not values:
        return []
    alpha = 2 / (period + 1)
    output = [values[0]]
    for value in values[1:]:
        output.append(value * alpha + output[-1] * (1 - alpha))
    return output


def macd_hist(closes: Sequence[float]) -> List[float]:
    fast = ema(closes, 12)
    slow = ema(closes, 26)
    diff = [left - right for left, right in zip(fast, slow)]
    signal = ema(diff, 9)
    return [left - right for left, right in zip(diff, signal)]


def same_color_macd_area(window: Sequence[float], direction: str) -> float:
    """下跌只累计绿柱面积，上涨只累计红柱面积，正负柱不互相抵消。"""
    if direction == "down":
        return float(sum(-value for value in window if value < 0))
    return float(sum(value for value in window if value > 0))


def direction_label(direction: str) -> str:
    return "上涨" if direction == "up" else "下跌"


def fmt_price(value: float) -> str:
    return f"{value:.2f}"
