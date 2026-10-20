"""在父浪时间区间内检验一层内部子浪。

用同一周期 K 线的更小 ZigZag 阈值作可复现的嵌套分解，不伪称更低时间周期数据。
父端点已由外层反向运动确认，可以作为内部边界；区间外的 K 线不参与内部划分。
只实现普通推动、之字形、平台形的一层检验；延长浪、楔形、三角形与组合不强行归类。
"""
from __future__ import annotations

from typing import Sequence

from .structure_bars import StructureBar

STATUS_LABELS = {
    "passed": "通过", "failed": "未通过", "insufficient": "分辨率不足",
    "unsupported": "超出支持形态", "unavailable": "缺少区间数据",
}


def _check_leg(bars, start, end, expected, threshold, label):
    from .elliott_wave import _price_of, impulse_checks, zigzag_pivots

    result = {"label": label, "expected": expected, "threshold": threshold,
              "start": start[1].time, "end": end[1].time, "observed": 0,
              "status": "unavailable", "checks": [], "pivots": []}
    # Match identity first: timestamps are not assumed unique or lexically sortable.
    indices = {id(bar): i for i, bar in enumerate(bars)}
    left, right = indices.get(id(start[1])), indices.get(id(end[1]))
    if left is None or right is None or right <= left:
        result["reason"] = "父浪端点不在原始K线序列中，或端点次序无效"
        return result
    window = bars[left:right + 1]
    result["bar_count"] = len(window)
    # Outside bars have unknown intrabar high/low order; never invent an ordering.
    if len(window) < expected + 1:
        result.update(status="insufficient", reason="K线数量不足以分辨所需子浪")
        return result
    fine, candidate = zigzag_pivots(window, threshold)
    if candidate is not None:
        fine = fine + [candidate]
    # Force no endpoints: only accept the decomposition if both observed extremes
    # coincide with the independently confirmed parent endpoints.
    if (not fine or fine[0][0] != start[0] or fine[-1][0] != end[0]
            or fine[0][1] is not start[1] or fine[-1][1] is not end[1]):
        result.update(status="insufficient", reason="更细摆动未能与父浪两端对齐")
        return result
    if any(a[1] is b[1] for a, b in zip(fine, fine[1:])):
        result.update(status="insufficient", reason="同一根K线内高低点先后不可辨")
        return result
    count = len(fine) - 1
    result.update(observed=count, pivots=[{
        "kind": kind, "time": bar.time, "price": _price_of((kind, bar)),
    } for kind, bar in fine])
    if count < expected:
        result.update(status="insufficient", reason=f"只分辨出{count}段，需要{expected}段，不能据此验证内部结构")
    elif count > expected:
        result.update(status="unsupported", reason=f"出现{count}段，可能涉及延长或复杂调整，本实现不自动压缩浪段")
    elif expected == 5:
        checks = impulse_checks(fine)
        result.update(checks=checks["rules"], status="passed" if checks["passed"] else "failed",
                      reason="五子浪价格规则通过" if checks["passed"] else "五子浪违反普通推动浪价格规则；未检验楔形替代计数")
    else:
        # A three must reverse then resume its net direction. Do not elevate
        # Fibonacci guidelines to rules or infer an unobserved 5-3-5 beneath it.
        sign = 1 if start[0] == "low" else -1
        p = [sign * _price_of(item) for item in fine]
        checks = [("子浪A顺父浪方向", p[1] > p[0]), ("子浪B反向", p[2] < p[1]),
                  ("子浪C顺父浪方向", p[3] > p[2]), ("净运动顺父浪方向", p[3] > p[0])]
        passed = all(ok for _, ok in checks)
        result.update(checks=checks, status="passed" if passed else "failed",
                      reason="三子浪方向规则通过；其下一级尚未递归检验" if passed else "三子浪方向规则未通过")
    return result


def validate_subwaves(bars: Sequence[StructureBar], points: Sequence, pattern: str,
                      threshold: float, shape: str = "") -> dict:
    """pattern 为 impulse 或 abc；通过仅表示当前分辨率下一层规则通过。"""
    expected = (5, 3, 5, 3, 5) if pattern == "impulse" else (
        (5, 3, 5) if shape == "之字形" else (3, 3, 5) if shape == "平台形" else ()
    )
    result = {"status": "unavailable", "method": "同周期K线、父级阈值的1/3、单层嵌套摆动",
              "depth": 1, "threshold": threshold / 3, "expected": list(expected), "legs": [],
              "limitations": ["未做跨时间周期对应", "未递归检验更深子浪", "未覆盖楔形、延长浪、三角形和组合调整"]}
    if threshold <= 0 or not expected or len(points) != len(expected) + 1:
        result.update(status="unsupported", reason="没有可检验的普通推动、之字形或平台形父结构")
        return result
    labels = [str(i + 1) for i in range(5)] if pattern == "impulse" else list("ABC")
    result["legs"] = [_check_leg(bars, a, b, n, threshold / 3, label)
                      for a, b, n, label in zip(points, points[1:], expected, labels)]
    statuses = {item["status"] for item in result["legs"]}
    result["status"] = next((s for s in ("failed", "unavailable", "insufficient", "unsupported") if s in statuses), "passed")
    result["reason"] = "一层内部子浪检验" + STATUS_LABELS[result["status"]]
    return result
