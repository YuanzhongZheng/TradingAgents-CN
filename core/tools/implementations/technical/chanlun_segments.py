"""特征序列线段（第 67 课口径）。端点时间与确认时间分别记录。

输入为按时间排列、相邻方向交替的笔端点；不把最后一个可变端点作为确认依据。
从样本内第一个满足三笔重叠的起点开始，不推断样本之前的线段或递归级别。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class Feature:
    low: float
    high: float
    low_index: int
    high_index: int
    observed: int


def _price(stroke):
    return stroke[2].high if stroke[0] == "top" else stroke[2].low


def standard_features(strokes: Sequence, start: int) -> list[Feature]:
    """上涨段取下降笔，下降段取上涨笔；包含合并保留两个极值的原始位置。"""
    output: list[Feature] = []
    direction = 1 if strokes[start][0] == "bottom" else -1
    for i in range(start + 1, len(strokes) - 1, 2):
        a, b = _price(strokes[i]), _price(strokes[i + 1])
        item = Feature(min(a, b), max(a, b), i if a < b else i + 1,
                       i if a > b else i + 1, i + 1)
        if output:
            prev = output[-1]
            included = ((item.low >= prev.low and item.high <= prev.high)
                        or (prev.low >= item.low and prev.high <= item.high))
            if included:
                high = max(prev.high, item.high) if direction > 0 else min(prev.high, item.high)
                low = max(prev.low, item.low) if direction > 0 else min(prev.low, item.low)
                output[-1] = Feature(
                    low, high, prev.low_index if low == prev.low else item.low_index,
                    prev.high_index if high == prev.high else item.high_index, item.observed,
                )
                continue
            direction = 1 if item.high > prev.high else -1
        output.append(item)
    return output


def _turns(strokes: Sequence, start: int):
    up = strokes[start][0] == "bottom"
    features = standard_features(strokes, start)
    for left, mid, right in zip(features, features[1:], features[2:]):
        top = mid.high > max(left.high, right.high) and mid.low > max(left.low, right.low)
        bottom = mid.low < min(left.low, right.low) and mid.high < min(left.high, right.high)
        if not (top if up else bottom):
            continue
        endpoint = mid.high_index if up else mid.low_index
        if endpoint - start < 3:
            continue
        # A feature turn behind a more extreme point cannot terminate this segment.
        prices = [_price(s) for s in strokes[start:right.observed + 1]]
        if _price(strokes[endpoint]) != (max(prices) if up else min(prices)):
            continue
        gap = mid.low > left.high if up else mid.high < left.low
        yield endpoint, right.observed, gap


def _initial_overlap(strokes: Sequence, start: int) -> bool:
    if start + 3 >= len(strokes):
        return False
    p = [_price(s) for s in strokes[start:start + 4]]
    return max(min(a, b) for a, b in zip(p, p[1:])) < min(max(a, b) for a, b in zip(p, p[1:]))


def _record(strokes, start, end, observed, gap, status):
    return {
        "direction": "up" if strokes[start][0] == "bottom" else "down",
        "start_stroke": start, "end_stroke": end, "stroke_count": end - start,
        "start": strokes[start][2].time, "end": strokes[end][2].time,
        "start_price": _price(strokes[start]), "end_price": _price(strokes[end]),
        "confirmed_at": strokes[observed][2].time if status == "confirmed" else None,
        "confirmed_stroke": observed if status == "confirmed" else None,
        "feature_gap": gap, "status": status,
        "rule": "缺口后反向特征序列分型" if gap else "无缺口特征序列分型",
    }


def chanlun_segments(strokes: Sequence, *, last_endpoint_confirmed: bool = False) -> dict:
    """按前缀逐步确认，已确认线段不因追加数据重画；缺口必须等反向序列分型。

    默认剔除可被同类分型替换的末笔端点。测试或调用方有独立确认依据时才可显式传 True。
    """
    stable = list(strokes if last_endpoint_confirmed else strokes[:-1])
    result = {"segments": [], "candidate": None, "skipped_strokes": 0}
    if len(stable) < 4:
        return result
    start = 0
    while start + 3 < len(stable) and not _initial_overlap(stable, start):
        start += 1
    result["skipped_strokes"] = start
    while start + 3 < len(stable):
        found = None
        pending = None
        for stop in range(start + 4, len(stable) + 1):
            prefix = stable[:stop]
            pending = None
            for end, observed, gap in _turns(prefix, start):
                if not _initial_overlap(prefix, start):
                    continue
                if gap:
                    up = prefix[start][0] == "bottom"
                    prices = [_price(s) for s in prefix[start:]]
                    if _price(prefix[end]) != (max(prices) if up else min(prices)):
                        continue
                    # Second feature fractal confirms case 2 regardless of its own gap.
                    reverse = next(_turns(prefix, end), None)
                    if reverse is None:
                        pending = _record(prefix, start, end, observed, True, "candidate")
                        continue
                    observed = max(observed, reverse[1])
                found = _record(prefix, start, end, observed, gap, "confirmed")
                break
            if found:
                break
        if found is None:
            if pending is None:
                up = stable[start][0] == "bottom"
                indices = range(start + 1, len(stable), 2)
                end = (max if up else min)(indices, key=lambda i: _price(stable[i]))
                pending = _record(stable, start, end, end, False, "candidate")
                pending["rule"] = "等待特征序列分型"
            result["candidate"] = pending
            break
        result["segments"].append(found)
        start = found["end_stroke"]
    if result["candidate"] is None and start + 1 < len(stable):
        up = stable[start][0] == "bottom"
        indices = range(start + 1, len(stable), 2)
        end = (max if up else min)(indices, key=lambda i: _price(stable[i]))
        result["candidate"] = _record(stable, start, end, end, False, "candidate")
        result["candidate"]["rule"] = "不足三笔，等待后续结构"
    if not last_endpoint_confirmed:
        for segment in result["segments"]:
            observed = segment["confirmed_stroke"] + 1
            segment["confirmed_stroke"] = observed
            segment["confirmed_at"] = strokes[observed][2].time
    return result
