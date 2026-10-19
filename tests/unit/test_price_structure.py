from core.tools.implementations.technical.price_structure import (
    DEFAULT_STRUCTURE_PERIODS,
    StructureBar,
    analyze_chanlun_bars,
    analyze_elliott_bars,
    merge_chanlun_bars,
    parse_structure_periods,
    render_price_structure,
)


def _bar(index: int, low: float, high: float) -> StructureBar:
    close = (low + high) / 2
    return StructureBar(time=f"2026-01-{index:02d}", open=close, high=high, low=low, close=close)


def test_default_periods_are_week_day_30m_5m():
    assert DEFAULT_STRUCTURE_PERIODS == ("week", "day", "30m", "5m")
    assert parse_structure_periods("周线,日线,30分钟,5分钟") == DEFAULT_STRUCTURE_PERIODS
    assert parse_structure_periods("") == DEFAULT_STRUCTURE_PERIODS


def test_chanlun_merges_included_bars_and_finds_strokes():
    included = [
        StructureBar("a", 10, 12, 9, 11),
        StructureBar("b", 10.5, 11.5, 9.5, 11),
    ]
    merged = merge_chanlun_bars(included)
    assert len(merged) == 1
    assert merged[0].high == 12
    assert merged[0].low == 9.5

    spans = [
        (5, 6), (4, 5), (3, 4),
        (4, 5), (6, 7), (8, 9), (10, 11), (12, 14),
        (10, 11), (8, 9), (6, 7), (5, 6), (4.5, 5.5),
        (6, 7), (8, 9), (10, 11), (11, 13),
        (9, 10), (7, 8), (6, 7), (5.5, 6.5),
        (7, 8), (9, 10), (11, 12), (13, 15),
    ]
    bars = [_bar(index, low, high) for index, (low, high) in enumerate(spans, start=1)]
    result = analyze_chanlun_bars(bars)
    assert result["status"] == "ok"
    assert len(result["moves"]) >= 3
    assert result["centers"]


def test_elliott_labels_an_upward_impulse():
    prices = [10, 12, 14, 18, 16, 14, 17, 21, 25, 23, 21, 24, 28, 32, 30, 28, 31, 35, 39, 37, 34, 36, 40, 44]
    bars = [_bar(index, price - 0.4, price + 0.4) for index, price in enumerate(prices, start=1)]
    result = analyze_elliott_bars(bars, threshold=0.08)
    assert result["status"] == "ok"
    assert result["pattern"] == "上升推动五浪"


def test_render_keeps_both_sections_for_four_periods():
    bars = [_bar(index, 10 + index * 0.2, 12 + index * 0.2) for index in range(1, 30)]
    payload = {
        period: {
            "source": "fixture",
            "error": "",
            "chanlun": analyze_chanlun_bars(bars),
            "elliott": analyze_elliott_bars(bars, 0.05),
        }
        for period in DEFAULT_STRUCTURE_PERIODS
    }
    text = render_price_structure("600519", payload)
    assert "缠论结构" in text
    assert "波浪结构" in text
    for label in ("周线", "日线", "30分钟", "5分钟"):
        assert text.count(label) >= 2
