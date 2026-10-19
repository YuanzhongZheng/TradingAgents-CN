"""波浪理论的规则化候选计数。

只在已确认的摆动拐点上检查推动浪的三条铁律和 ABC 的常见比例。
末端未确认的拐点单独给出暂定计数，不混进已确认结果。内部子浪没有检验。
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from .structure_bars import StructureBar, direction_label, fmt_price

Pivot = Tuple[str, StructureBar]

WAVE_UNVERIFIED = (
    "未检验内部子浪",
    "未做跨周期对应",
    "不是已确认的波浪计数",
)


def zigzag_pivots(bars: Sequence[StructureBar], threshold: float) -> Tuple[List[Pivot], Optional[Pivot]]:
    """返回已确认拐点和末端候选拐点。

    起点取第一段达到阈值之前的真实最低点或最高点。只有反向运动达到阈值，
    极值才算已确认；最后一段仍在延伸时，末端极值保持为候选。
    """
    if len(bars) < 3 or threshold <= 0:
        return [], None
    pivots: List[Pivot] = []
    mode: Optional[str] = None
    extreme = 0
    low_index = 0
    high_index = 0
    for index in range(1, len(bars)):
        bar = bars[index]
        if mode is None:
            if bar.low < bars[low_index].low:
                low_index = index
            if bar.high > bars[high_index].high:
                high_index = index
            lowest = bars[low_index].low
            highest = bars[high_index].high
            up_move = (bar.high - lowest) / lowest if lowest > 0 else 0.0
            down_move = (highest - bar.low) / highest if highest > 0 else 0.0
            up_ready = up_move >= threshold
            down_ready = down_move >= threshold
            if not (up_ready or down_ready):
                continue
            if up_ready and (not down_ready or low_index <= high_index):
                pivots.append(("low", bars[low_index]))
                mode = "up"
                extreme = max(range(low_index, index + 1), key=lambda item: bars[item].high)
            else:
                pivots.append(("high", bars[high_index]))
                mode = "down"
                extreme = min(range(high_index, index + 1), key=lambda item: bars[item].low)
            continue
        if mode == "up":
            if bar.high >= bars[extreme].high:
                extreme = index
                continue
            if bars[extreme].high > 0 and (bars[extreme].high - bar.low) / bars[extreme].high >= threshold:
                pivots.append(("high", bars[extreme]))
                mode = "down"
                extreme = index
        else:
            if bar.low <= bars[extreme].low:
                extreme = index
                continue
            if bars[extreme].low > 0 and (bar.high - bars[extreme].low) / bars[extreme].low >= threshold:
                pivots.append(("low", bars[extreme]))
                mode = "up"
                extreme = index
    candidate = None
    if mode == "up":
        candidate = ("high", bars[extreme])
    elif mode == "down":
        candidate = ("low", bars[extreme])
    return pivots, candidate


def _price_of(pivot: Pivot) -> float:
    kind, bar = pivot
    return bar.high if kind == "high" else bar.low


def _wave3_not_shortest(wave1: float, wave3: float, wave5: float) -> bool:
    """第 3 浪不能是 1、3、5 浪中最短的一浪，但不必是最长的。"""
    if wave1 <= 0 or wave3 <= 0 or wave5 <= 0:
        return False
    return not (wave3 < wave1 and wave3 < wave5)


def _ratio(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


def impulse_checks(points: Sequence[Pivot]) -> Dict[str, object]:
    """检查六个拐点能否构成推动五浪。下降推动先取相反数，再按上升规则检查。"""
    bullish = points[0][0] == "low"
    sign = 1.0 if bullish else -1.0
    p = [sign * _price_of(point) for point in points]
    wave1 = p[1] - p[0]
    wave2 = p[1] - p[2]
    wave3 = p[3] - p[2]
    wave4 = p[3] - p[4]
    wave5 = p[5] - p[4]
    rules = [
        ("第2浪没有回到第1浪起点", p[2] > p[0]),
        ("第3浪越过第1浪终点", p[3] > p[1]),
        ("第3浪不是1、3、5浪中最短的一浪", _wave3_not_shortest(wave1, wave3, wave5)),
        ("第4浪没有进入第1浪价格区间", p[4] > p[1]),
    ]
    retrace2 = _ratio(wave2, wave1)
    retrace4 = _ratio(wave4, wave3)
    ratio3 = _ratio(wave3, wave1)
    ratio5 = _ratio(wave5, wave1)
    alternation = (retrace2 >= 0.5 and retrace4 < 0.382) or (retrace4 >= 0.5 and retrace2 < 0.382)
    guidelines = [
        ("第2浪回撤第1浪", f"{retrace2:.1%}", 0.382 <= retrace2 <= 0.786, "常见 38.2%–78.6%"),
        ("第3浪是第1浪的倍数", f"{ratio3:.2f} 倍", ratio3 >= 1.0, "常见不少于 1 倍"),
        ("第4浪回撤第3浪", f"{retrace4:.1%}", 0.236 <= retrace4 <= 0.5, "常见 23.6%–50%"),
        ("第5浪是第1浪的倍数", f"{ratio5:.2f} 倍", 0.618 <= ratio5 <= 1.618, "常见 0.618–1.618 倍"),
        ("第2、4浪回撤深浅交替", f"{retrace2:.1%} / {retrace4:.1%}", alternation, "一深一浅较常见"),
    ]
    return {
        "bullish": bullish,
        "rules": rules,
        "passed": all(ok for _name, ok in rules) and min(wave1, wave2, wave3, wave4, wave5) > 0,
        "guidelines": guidelines,
        "fifth_extended": p[5] > p[3],
    }


def abc_checks(points: Sequence[Pivot]) -> Dict[str, object]:
    """检查最后四个拐点能否看作 ABC。比例不合常见范围时不叫 ABC。"""
    a_up = points[0][0] == "low"
    sign = 1.0 if a_up else -1.0
    p = [sign * _price_of(point) for point in points]
    wave_a = p[1] - p[0]
    wave_b = p[1] - p[2]
    wave_c = p[3] - p[2]
    retrace_b = _ratio(wave_b, wave_a)
    ratio_c = _ratio(wave_c, wave_a)
    valid = wave_a > 0 and 0.382 <= retrace_b <= 1.382 and ratio_c >= 0.618
    shape = "未细分"
    if valid and retrace_b < 0.786 and p[3] > p[1]:
        shape = "之字形"
    elif valid and retrace_b >= 0.786:
        shape = "平台形"
    return {
        "valid": valid,
        "direction": "up" if a_up else "down",
        "shape": shape,
        "guidelines": [
            ("B浪回撤A浪", f"{retrace_b:.1%}", 0.382 <= retrace_b <= 1.382, "常见 38.2%–138.2%"),
            ("C浪是A浪的倍数", f"{ratio_c:.2f} 倍", ratio_c >= 0.618, "常见不少于 0.618 倍"),
        ],
    }


def _build_waves(points: Sequence[Pivot], labels: Sequence[str]) -> List[Dict[str, object]]:
    waves = []
    for index in range(len(points) - 1):
        start_kind, start_bar = points[index]
        _end_kind, end_bar = points[index + 1]
        waves.append({
            "label": labels[index] if index < len(labels) else "",
            "start": start_bar.time,
            "end": end_bar.time,
            "start_price": _price_of(points[index]),
            "end_price": _price_of(points[index + 1]),
            "direction": "up" if start_kind == "low" else "down",
        })
    return waves


def _label_elliott(pivots: Sequence[Pivot]) -> Dict[str, object]:
    """在已确认拐点上找最近的推动五浪候选；找不到时再检查最后四点是否像 ABC。"""
    if len(pivots) < 4:
        return {"pattern": "样本不足", "waves": [], "rules": [], "guidelines": [], "unverified": list(WAVE_UNVERIFIED)}
    for offset in range(0, 4):
        end = len(pivots) - offset
        start = end - 6
        if start < 0:
            break
        points = pivots[start:end]
        checks = impulse_checks(points)
        if not checks["passed"]:
            continue
        rising = checks["bullish"]
        pattern = "上升推动五浪候选" if rising else "下降推动五浪候选"
        if not checks["fifth_extended"]:
            pattern += "，第5浪未创新高" if rising else "，第5浪未创新低"
        if offset:
            pattern += f"，已在 {points[-1][1].time} 结束，之后出现 {offset} 段反向摆动"
            if offset == 3:
                follow = abc_checks(pivots[end - 1:])
                if follow["valid"]:
                    pattern += f"，这三段可看作ABC调整候选（{follow['shape']}）"
        return {
            "pattern": pattern,
            "waves": _build_waves(points, ["1", "2", "3", "4", "5"]),
            "rules": checks["rules"],
            "guidelines": checks["guidelines"],
            "unverified": list(WAVE_UNVERIFIED),
        }
    points = pivots[-4:]
    checks = abc_checks(points)
    if checks["valid"]:
        pattern = "ABC调整候选"
        shape = checks["shape"]
    else:
        pattern = "摆动候选（不满足推动浪规则，也不满足ABC常见比例）"
        shape = ""
    return {
        "pattern": pattern,
        "shape": shape,
        "waves": _build_waves(points, ["A", "B", "C"]),
        "rules": [],
        "guidelines": checks["guidelines"],
        "unverified": list(WAVE_UNVERIFIED),
    }


def analyze_elliott_bars(bars: Sequence[StructureBar], threshold: float) -> Dict[str, object]:
    if len(bars) < 20:
        return {"status": "insufficient", "bar_count": len(bars)}
    pivots, candidate = zigzag_pivots(bars, threshold)
    labeled = _label_elliott(pivots)
    core_pattern = str(labeled["pattern"])
    pattern = core_pattern
    terminal_status = "confirmed"
    provisional = None
    developing = None
    if candidate is not None:
        terminal_status = "candidate"
        kind = "高点" if candidate[0] == "high" else "低点"
        provisional_label = _label_elliott(list(pivots) + [candidate])
        provisional_pattern = str(provisional_label["pattern"])
        if provisional_pattern != core_pattern:
            provisional = provisional_pattern
        if pivots:
            start_price = _price_of(pivots[-1])
            end_price = _price_of(candidate)
            developing = {
                "direction": "up" if candidate[0] == "high" else "down",
                "start": pivots[-1][1].time,
                "start_price": start_price,
                "end": candidate[1].time,
                "end_price": end_price,
                "change": (end_price - start_price) / start_price if start_price else 0.0,
            }
        if provisional and "推动五浪候选" in provisional and "已在" not in provisional:
            pattern = (
                f"{core_pattern}。末端{kind}是候选拐点，反向运动尚未达到阈值；暂定计数为{provisional}，"
                "但不把它当作已完成的第5浪终点，第5浪可能仍在延伸"
            )
        else:
            pattern = (
                f"{core_pattern}。末端{kind}是候选拐点，反向运动尚未达到阈值，"
                "当前这一段可能仍在延伸，不计入已确认浪段"
            )
    return {
        "status": "ok",
        "bar_count": len(bars),
        "threshold": threshold,
        "pivots": list(pivots[-8:]),
        "candidate_pivot": candidate,
        "terminal_status": terminal_status,
        "core_pattern": core_pattern,
        "pattern": pattern,
        "provisional_pattern": provisional,
        "developing": developing,
        "waves": labeled["waves"],
        "shape": labeled.get("shape", ""),
        "rules": labeled["rules"],
        "guidelines": labeled["guidelines"],
        "unverified": labeled["unverified"],
        "latest_time": bars[-1].time,
        "latest_close": bars[-1].close,
    }


def render_elliott(label: str, result: Dict[str, object], source: str = "") -> str:
    if result.get("status") == "unavailable":
        return f"### {label}\n该周期在分析截止日期前没有可用行情，不使用截止日期之后的数据。"
    if result.get("status") != "ok":
        return f"### {label}\n样本不足（{result.get('bar_count', 0)} 根），不划分浪。"
    lines = [
        f"### {label}",
        f"- K线 {result['bar_count']} 根，摆动阈值 {result['threshold']:.0%}，"
        f"最新收盘 {fmt_price(result['latest_close'])}（{result['latest_time']}）",
        f"- 候选形态: {result['pattern']}",
        "- 未验证: " + "；".join(result.get("unverified") or WAVE_UNVERIFIED),
    ]
    if source:
        lines.append(f"- 数据源: {source}")
    if result.get("shape"):
        lines.append(f"- ABC形状: {result['shape']}候选")
    rules = result.get("rules") or []
    if rules:
        lines.append("- 推动浪规则: " + "；".join(f"{name}{'（满足）' if ok else '（不满足）'}" for name, ok in rules))
    guidelines = result.get("guidelines") or []
    if guidelines:
        lines.append("- 常见比例（只是经验范围，不是规则）:")
        for name, value, within, typical in guidelines:
            lines.append(f"  - {name} {value}，{typical}，{'在范围内' if within else '不在范围内'}")
    pivots = result.get("pivots") or []
    if pivots:
        text = " → ".join(
            f"{'高' if kind == 'high' else '低'} {fmt_price(_price_of((kind, bar)))}@{bar.time}"
            for kind, bar in pivots
        )
        lines.append(f"- 已确认拐点: {text}")
    candidate = result.get("candidate_pivot")
    developing = result.get("developing")
    if candidate:
        kind, bar = candidate
        lines.append(
            f"- 末端候选拐点: {'高' if kind == 'high' else '低'} {fmt_price(_price_of(candidate))}@{bar.time}（未确认）"
        )
        if developing:
            lines.append(
                f"- 进行中的一段: {direction_label(developing['direction'])} {developing['start']} "
                f"{fmt_price(developing['start_price'])} → {developing['end']} "
                f"{fmt_price(developing['end_price'])}（{developing['change']:+.1%}）"
            )
    elif result.get("terminal_status") == "confirmed":
        lines.append("- 末端拐点: 已由反向阈值确认")
    waves = result.get("waves") or []
    if waves:
        lines.append("- 浪段:")
        for wave in waves:
            lines.append(
                f"  - {wave['label']} {direction_label(wave['direction'])} {wave['start']} "
                f"{fmt_price(wave['start_price'])} → {wave['end']} {fmt_price(wave['end_price'])}"
            )
    return "\n".join(lines)
