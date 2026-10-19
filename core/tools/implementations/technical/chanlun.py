"""缠论笔与三笔重叠区间的规则化计算。

这里只做到笔级别：包含处理、分型、笔、三笔重叠近似区间，以及以区间为背景的
进入段和离开段力度比较。没有线段划分和级别联立，结果只能作为候选。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Dict, List, Optional, Sequence, Tuple

from .structure_bars import (
    StructureBar,
    direction_label,
    fmt_price,
    macd_hist,
    same_color_macd_area,
)

Stroke = Tuple[str, int, StructureBar]

CHANLUN_LIMITATION = "只有笔和三笔重叠近似，没有线段、级别联立，背驰只到候选"


def merge_chanlun_bars(bars: Sequence[StructureBar]) -> List[StructureBar]:
    """处理 K 线包含关系。向上取高高，向下取低低，并记录合并覆盖的原始 K 线范围。"""
    if not bars:
        return []
    merged = [replace(bars[0], start_index=0, end_index=0)]
    direction = 0
    for position, bar in enumerate(bars[1:], start=1):
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
            merged.append(replace(bar, start_index=position, end_index=position))
            continue
        if direction >= 0:
            high = max(previous.high, bar.high)
            low = max(previous.low, bar.low)
            extreme_time = bar.time if bar.high >= previous.high else previous.time
        else:
            high = min(previous.high, bar.high)
            low = min(previous.low, bar.low)
            extreme_time = bar.time if bar.low <= previous.low else previous.time
        merged[-1] = StructureBar(
            time=extreme_time or bar.time or previous.time,
            open=previous.open,
            high=high,
            low=low,
            close=bar.close,
            volume=previous.volume + bar.volume,
            start_index=previous.start_index,
            end_index=position,
        )
    return merged


def chanlun_fractals(bars: Sequence[StructureBar]) -> List[Stroke]:
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


def _valid_stroke(start_kind: str, start: StructureBar, end: StructureBar) -> bool:
    """顶分型必须整体高于相邻底分型，否则这一笔没有实际的上下空间。"""
    if start_kind == "bottom":
        return end.high > start.high and end.low > start.low
    return end.low < start.low and end.high < start.high


def chanlun_bi(fractals: Sequence[Stroke]) -> List[Stroke]:
    """相邻顶底分型之间至少隔一根独立 K 线才构成一笔，同类分型保留更极端的一个。"""
    strokes: List[Stroke] = []
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
        if not _valid_stroke(last_kind, last_bar, bar):
            continue
        strokes.append((kind, index, bar))
    return strokes


def _stroke_segments(strokes: Sequence[Stroke]) -> List[Tuple[float, float]]:
    segments = []
    for index in range(len(strokes) - 1):
        start = strokes[index][2]
        end = strokes[index + 1][2]
        segments.append((min(start.low, end.low), max(start.high, end.high)))
    return segments


def _ranges_overlap(left: Tuple[float, float], right: Tuple[float, float]) -> bool:
    return min(left[1], right[1]) > max(left[0], right[0])


def chanlun_zhongshu(strokes: Sequence[Stroke]) -> List[Dict[str, object]]:
    """三笔重叠的近似区间。

    区间上下沿由形成时的三笔决定。之后仍与区间重叠的笔记为延伸，完全离开记为离开。
    新区间与前一个区间相交且更宽时记为扩展。这不是线段中枢，也不处理级别联立。
    """
    segments = _stroke_segments(strokes)
    centers: List[Dict[str, object]] = []
    cursor = 0
    while cursor + 2 < len(segments):
        lows = [segments[cursor + offset][0] for offset in range(3)]
        highs = [segments[cursor + offset][1] for offset in range(3)]
        lower = max(lows)
        upper = min(highs)
        if upper <= lower:
            cursor += 1
            continue
        end = cursor + 3
        state = "形成"
        previous = centers[-1] if centers else None
        if previous and _ranges_overlap((lower, upper), (previous["lower"], previous["upper"])):
            if lower < previous["lower"] or upper > previous["upper"]:
                state = "扩展"
        while end < len(segments):
            seg_low, seg_high = segments[end]
            if seg_high <= lower or seg_low >= upper:
                state = "离开"
                break
            if state != "扩展":
                state = "延伸"
            end += 1
        covered = segments[cursor:end]
        centers.append({
            "lower": lower,
            "upper": upper,
            "highest": max(item[1] for item in covered),
            "lowest": min(item[0] for item in covered),
            "start_stroke": cursor + 1,
            "end_stroke": end,
            "first_move": cursor,
            "last_move": end - 1,
            "state": state,
            "kind": "三笔重叠近似",
        })
        if state != "离开":
            break
        cursor = end
    return centers


def _stroke_moves(strokes: Sequence[Stroke], hist: Sequence[float]) -> List[Dict[str, object]]:
    moves = []
    for index in range(len(strokes) - 1):
        start_kind, _start_index, start = strokes[index]
        _end_kind, _end_index, end = strokes[index + 1]
        direction = "down" if start_kind == "top" else "up"
        start_price = start.high if direction == "down" else start.low
        end_price = end.low if direction == "down" else end.high
        raw_start = max(start.start_index, 0)
        raw_end = max(end.end_index, raw_start)
        window = hist[raw_start:raw_end + 1]
        bar_span = raw_end - raw_start + 1
        price_range = abs(end_price - start_price)
        moves.append({
            "direction": direction,
            "start": start.time,
            "end": end.time,
            "start_price": start_price,
            "end_price": end_price,
            "range": price_range,
            "change": (end_price - start_price) / start_price if start_price else 0.0,
            "bars": bar_span,
            "speed": price_range / bar_span if bar_span else 0.0,
            "macd_area": same_color_macd_area(window, direction),
        })
    return moves


def _momentum_note(moves: Sequence[Dict[str, object]]) -> str:
    """相邻同向笔的比较。幅度和同向动能柱缩小，最多说明动能减弱，不能单独称为背驰。"""
    grouped: Dict[str, List[Dict[str, object]]] = {}
    for move in moves:
        grouped.setdefault(str(move["direction"]), []).append(move)
    notes = []
    for direction, extreme_name in (("down", "新低"), ("up", "新高")):
        group = grouped.get(direction) or []
        if len(group) < 2:
            continue
        previous, latest = group[-2], group[-1]
        made_extreme = (
            float(latest["end_price"]) < float(previous["end_price"])
            if direction == "down"
            else float(latest["end_price"]) > float(previous["end_price"])
        )
        range_smaller = float(latest["range"]) < float(previous["range"]) and float(previous["range"]) > 0
        macd_smaller = float(latest["macd_area"]) < float(previous["macd_area"])
        side = direction_label(direction)
        if range_smaller and macd_smaller and made_extreme:
            notes.append(
                f"{side}笔的摆动幅度和同向动能柱面积都缩小，且价格创{extreme_name}；"
                "这只是相邻笔的比较，不能单独称为背驰"
            )
        elif range_smaller and not made_extreme:
            notes.append(f"{side}笔幅度变小，但没有创{extreme_name}，不构成背驰")
    if not notes:
        return "相邻同向笔没有出现幅度和动能同时缩小的情况"
    return "；".join(notes)


def center_background(centers: Sequence[Dict[str, object]]) -> Tuple[str, str]:
    if not centers:
        return "none", "尚未形成三笔重叠区间"
    if len(centers) == 1:
        return "range", "只有一个重叠区间，按盘整背景处理"
    previous, last = centers[-2], centers[-1]
    if float(last["lower"]) > float(previous["upper"]):
        return "up", "最近两个重叠区间依次抬高且互不重叠，按上涨背景处理"
    if float(last["upper"]) < float(previous["lower"]):
        return "down", "最近两个重叠区间依次降低且互不重叠，按下跌背景处理"
    return "range", "最近两个重叠区间有重叠，按盘整背景处理"


def center_divergence(
    moves: Sequence[Dict[str, object]],
    centers: Sequence[Dict[str, object]],
    background: str,
    last_extending: bool = False,
) -> str:
    """以最近重叠区间为背景，比较进入段和离开段的力度。"""
    if not centers:
        return "尚未形成重叠区间，无法做区间背景下的力度比较"
    center = centers[-1]
    first = int(center["first_move"])
    last = int(center["last_move"])
    if center["state"] != "离开" or last + 1 >= len(moves):
        return "价格还没有离开最近重叠区间，进入段与离开段的比较条件尚未出现"
    if first == 0:
        return "最近重叠区间之前没有完整的进入段，无法比较"
    if last_extending and last + 1 == len(moves) - 1:
        return "离开段仍在延伸，终点未定，暂不比较进入段与离开段的力度"
    entering = moves[first - 1]
    leaving = moves[last + 1]
    if entering["direction"] != leaving["direction"]:
        return (
            f"进入段为{direction_label(entering['direction'])}、离开段为"
            f"{direction_label(leaving['direction'])}，方向不同，不做背驰比较"
        )
    direction = str(leaving["direction"])
    side = direction_label(direction)
    point = "低点" if direction == "down" else "高点"
    made_extreme = (
        float(leaving["end_price"]) < float(entering["end_price"])
        if direction == "down"
        else float(leaving["end_price"]) > float(entering["end_price"])
    )
    if not made_extreme:
        return f"{side}离开段没有越过进入段的{point}，不构成背驰"
    entering_area = float(entering["macd_area"])
    leaving_area = float(leaving["macd_area"])
    if leaving_area >= entering_area:
        return (
            f"{side}离开段越过进入段{point}，同向MACD柱面积没有缩小"
            f"（{entering_area:.2f} → {leaving_area:.2f}），不构成背驰"
        )
    kind = "趋势背驰候选" if background == direction else "盘整背驰候选"
    speed_note = "，单根K线平均幅度也变小" if float(leaving["speed"]) < float(entering["speed"]) else ""
    return (
        f"{kind}：{side}离开段越过进入段{point}，同向MACD柱面积从 {entering_area:.2f} "
        f"降到 {leaving_area:.2f}{speed_note}。这是笔级别近似，未经线段和次级别确认"
    )


def _pending_stroke(strokes: Sequence[Stroke], merged: Sequence[StructureBar], bars: Sequence[StructureBar]):
    """末笔终点之后的走势。价格已越过终点时，末笔仍在延伸，不能当作完成的一笔比较。"""
    if not strokes:
        return None
    kind, index, bar = strokes[-1]
    price = bar.high if kind == "top" else bar.low
    after = merged[index + 1:]
    if kind == "top":
        extending = any(item.high > price for item in after)
    else:
        extending = any(item.low < price for item in after)
    latest = bars[-1]
    return {
        "time": bar.time,
        "price": price,
        "kind": "顶分型" if kind == "top" else "底分型",
        "bars_after": len(after),
        "change": (latest.close - price) / price if price else 0.0,
        "extending": extending,
    }


def _center_position(centers: Sequence[Dict[str, object]], close: float) -> str:
    if not centers:
        return "没有重叠区间可对照"
    last = centers[-1]
    if close > float(last["upper"]):
        return "最新收盘位于最近重叠区间上方"
    if close < float(last["lower"]):
        return "最新收盘位于最近重叠区间下方"
    return "最新收盘位于最近重叠区间内部"


def analyze_chanlun_bars(bars: Sequence[StructureBar]) -> Dict[str, object]:
    if len(bars) < 20:
        return {"status": "insufficient", "bar_count": len(bars)}
    merged = merge_chanlun_bars(bars)
    fractals = chanlun_fractals(merged)
    strokes = chanlun_bi(fractals)
    centers = chanlun_zhongshu(strokes)
    hist = macd_hist([bar.close for bar in bars])
    moves = _stroke_moves(strokes, hist)
    background, background_text = center_background(centers)
    pending = _pending_stroke(strokes, merged, bars)
    extending = bool(pending and pending["extending"])
    if extending and moves:
        moves[-1]["extending"] = True
    finished_moves = moves[:-1] if extending else moves
    return {
        "status": "ok",
        "bar_count": len(bars),
        "merged_count": len(merged),
        "strokes": strokes,
        "moves": moves[-6:],
        "centers": centers[-2:],
        "background": background,
        "background_text": background_text,
        "center_divergence": center_divergence(moves, centers, background, extending),
        "divergence": _momentum_note(finished_moves),
        "pending": pending,
        "center_position": _center_position(centers, bars[-1].close),
        "last_direction": moves[-1]["direction"] if moves else None,
        "latest_time": bars[-1].time,
        "latest_close": bars[-1].close,
        "limitation": CHANLUN_LIMITATION,
    }


def render_chanlun(label: str, result: Dict[str, object], source: str = "") -> str:
    if result.get("status") == "unavailable":
        return f"### {label}\n该周期在分析截止日期前没有可用行情，不使用截止日期之后的数据。"
    if result.get("status") != "ok":
        return f"### {label}\n样本不足（{result.get('bar_count', 0)} 根），不划分笔和重叠区间。"
    lines = [
        f"### {label}",
        f"- K线 {result['bar_count']} 根，包含处理后 {result['merged_count']} 根，"
        f"最新收盘 {fmt_price(result['latest_close'])}（{result['latest_time']}）",
        f"- 限制: {result.get('limitation', CHANLUN_LIMITATION)}",
    ]
    if source:
        lines.append(f"- 数据源: {source}")
    moves = result.get("moves") or []
    if moves:
        lines.append("- 最近的笔:")
        for move in moves:
            suffix = "，仍在延伸" if move.get("extending") else ""
            lines.append(
                f"  - {direction_label(move['direction'])} {move['start']} {fmt_price(move['start_price'])} → "
                f"{move['end']} {fmt_price(move['end_price'])}（{move['change']:+.1%}，"
                f"{move['bars']} 根K线，同向MACD柱面积 {move['macd_area']:.2f}{suffix}）"
            )
    else:
        lines.append("- 最近的笔: 未形成有效笔")
    pending = result.get("pending")
    if pending:
        if pending.get("extending"):
            state = "之后价格已越过该点，这一笔仍在延伸，终点会后移，不参与力度比较"
        else:
            state = "仍待确认，尚未形成反向笔"
        lines.append(
            f"- 末笔终点: {pending['kind']} {pending['time']} {fmt_price(pending['price'])}；"
            f"之后 {pending['bars_after']} 根合并K线，最新收盘相对该点 {pending['change']:+.1%}，{state}"
        )
    centers = result.get("centers") or []
    if centers:
        last = centers[-1]
        lines.append(
            f"- 三笔重叠近似区间: {fmt_price(last['lower'])} - {fmt_price(last['upper'])}"
            f"（{last.get('state', '形成')}，区间内波动 {fmt_price(last['lowest'])} - "
            f"{fmt_price(last['highest'])}，非完整线段中枢）"
        )
    else:
        lines.append("- 三笔重叠近似区间: 未形成")
    lines.append(f"- 位置: {result['center_position']}")
    lines.append(f"- 走势背景: {result['background_text']}")
    lines.append(f"- 区间背景下的力度比较: {result['center_divergence']}")
    lines.append(f"- 相邻同向笔: {result['divergence']}")
    return "\n".join(lines)
