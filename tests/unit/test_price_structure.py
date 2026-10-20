from core.tools.implementations.technical.chanlun import (
    _momentum_note,
    _pending_stroke,
    _valid_stroke,
    analyze_chanlun_bars,
    center_background,
    center_divergence,
    chanlun_zhongshu,
    merge_chanlun_bars,
)
from core.tools.implementations.technical.elliott_wave import (
    _label_elliott,
    abc_checks,
    analyze_elliott_bars,
    impulse_checks,
    zigzag_pivots,
)
from core.tools.implementations.technical.price_structure import (
    DEFAULT_STRUCTURE_PERIODS,
    PERIOD_LIMITS,
    analyze_price_structure,
    clear_structure_cache,
    parse_structure_periods,
    render_cross_period,
    render_price_structure,
)
from core.tools.implementations.technical.structure_bars import (
    StructureBar,
    same_color_macd_area,
)


def _bar(index: int, low: float, high: float) -> StructureBar:
    close = (low + high) / 2
    return StructureBar(time=f"2026-01-{index:02d}", open=close, high=high, low=low, close=close)


def _bars_from_anchors(anchors, steps=4, spread=0.1):
    prices = []
    for start, end in zip(anchors, anchors[1:]):
        for step in range(steps):
            prices.append(start + (end - start) * step / steps)
    prices.append(anchors[-1])
    return [
        StructureBar(f"T{index:03d}", price, price + spread, price - spread, price)
        for index, price in enumerate(prices, start=1)
    ]


def _pivot(kind: str, price: float, time: str) -> tuple:
    if kind == "high":
        bar = StructureBar(time, price, price, price - 1, price)
    else:
        bar = StructureBar(time, price, price + 1, price, price)
    return (kind, bar)


def _pivots(prices, first="low"):
    kinds = ("low", "high") if first == "low" else ("high", "low")
    return [_pivot(kinds[index % 2], price, str(index)) for index, price in enumerate(prices)]


def _strokes_from_endpoints(prices):
    strokes = []
    for index, price in enumerate(prices):
        kind = "bottom" if index % 2 == 0 else "top"
        bar = StructureBar(str(index), price, price, price, price)
        strokes.append((kind, index * 5, bar))
    return strokes


def _move(direction, end_price, area, price_range=10.0, speed=1.0):
    return {
        "direction": direction,
        "end_price": end_price,
        "macd_area": area,
        "range": price_range,
        "speed": speed,
    }


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
    assert (merged[0].start_index, merged[0].end_index) == (0, 1)

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
    assert result["pending"]["kind"] in ("顶分型", "底分型")
    assert "重叠区间" in result["center_position"]


def test_stroke_needs_top_above_bottom():
    bottom = StructureBar("b", 10, 11, 9, 10)
    low_top = StructureBar("t", 10, 10.5, 8.5, 9)
    high_top = StructureBar("t", 13, 14, 12, 13)
    assert not _valid_stroke("bottom", bottom, low_top)
    assert _valid_stroke("bottom", bottom, high_top)


def test_wave_three_need_not_be_the_longest():
    up = _label_elliott(_pivots([100, 120, 110, 125, 122, 132], "low"))
    down = _label_elliott(_pivots([132, 122, 125, 110, 120, 100], "high"))
    assert up["pattern"] == "上升推动五浪候选"
    assert down["pattern"] == "下降推动五浪候选"
    assert "未检验内部子浪" in up["unverified"]
    assert all(ok for _name, ok in up["rules"])


def test_impulse_checks_report_rule_failures_and_ratios():
    checks = impulse_checks(_pivots([100, 120, 110, 125, 118, 132], "low"))
    failed = [name for name, ok in checks["rules"] if not ok]
    assert failed == ["第4浪没有进入第1浪价格区间"]
    assert not checks["passed"]
    assert len(checks["guidelines"]) == 5
    assert checks["guidelines"][0][1] == "50.0%"


def test_abc_needs_common_ratios():
    valid = _label_elliott(_pivots([20, 10, 15, 6], "high"))
    assert valid["pattern"] == "ABC调整候选"
    assert valid["shape"] == "之字形"
    assert "不是已确认的波浪计数" in valid["unverified"]

    weak = _label_elliott(_pivots([10, 14, 12, 13], "low"))
    assert weak["pattern"].startswith("摆动候选")
    assert not abc_checks(_pivots([10, 14, 12, 13], "low"))["valid"]


def test_finished_impulse_followed_by_abc_is_reported():
    pivots = _pivots([100, 120, 110, 125, 122, 132, 118, 126, 112], "low")
    labeled = _label_elliott(pivots)
    assert labeled["pattern"].startswith("上升推动五浪候选，已在")
    assert "之后出现 3 段反向摆动" in labeled["pattern"]
    assert "ABC调整候选（之字形）" in labeled["pattern"]


def test_zigzag_starts_from_the_real_extreme():
    prices = [10, 9.5, 9, 8.5, 8, 8.5, 9, 9.5, 10, 10.5, 11, 11.5, 12]
    bars = [StructureBar(str(index), price, price, price, price) for index, price in enumerate(prices)]
    pivots, candidate = zigzag_pivots(bars, 0.3)
    assert pivots[0][0] == "low"
    assert pivots[0][1].low == 8
    assert candidate[0] == "high"


def test_unconfirmed_terminal_high_is_not_a_completed_fifth_wave():
    bars = _bars_from_anchors([100, 120, 110, 125, 122, 132])
    result = analyze_elliott_bars(bars, threshold=0.02)
    assert result["terminal_status"] == "candidate"
    assert result["candidate_pivot"][1].time == bars[-1].time
    assert result["pivots"][-1][1].time != bars[-1].time
    assert "推动五浪" not in result["core_pattern"]
    assert result["provisional_pattern"] == "上升推动五浪候选"
    assert "不把它当作已完成的第5浪终点" in result["pattern"]
    assert "仍在延伸" in result["pattern"]
    assert result["developing"]["direction"] == "up"


def test_unconfirmed_terminal_leg_stays_out_of_confirmed_waves():
    prices = [10, 11, 12, 14, 16, 18, 16, 14, 16, 18, 20, 22, 20, 18, 22, 26, 30, 34, 38, 42]
    bars = [_bar(index, price - 0.2, price + 0.2) for index, price in enumerate(prices, start=1)]
    result = analyze_elliott_bars(bars, threshold=0.08)
    assert result["terminal_status"] == "candidate"
    assert "候选拐点" in result["pattern"]
    assert "仍在延伸" in result["pattern"]


def test_higher_low_is_not_called_divergence():
    note = _momentum_note([
        {"direction": "down", "range": 15, "macd_area": 10, "end_price": 15},
        {"direction": "down", "range": 5, "macd_area": 2, "end_price": 20},
    ])
    assert "疑似背驰" not in note
    assert "不构成背驰" in note
    assert "新低" in note


def test_macd_area_uses_one_color():
    assert same_color_macd_area([10, -3, 4], "down") == 3
    assert same_color_macd_area([10, -3, 4], "up") == 14


def _center(state="离开"):
    return {"lower": 16, "upper": 22, "first_move": 1, "last_move": 3, "state": state}


def _center_moves(leaving_end, leaving_area):
    return [
        _move("down", 15, 10.0, price_range=15, speed=2.0),
        _move("up", 22, 3.0),
        _move("down", 16, 3.0),
        _move("up", 21, 3.0),
        _move("down", leaving_end, leaving_area, price_range=9, speed=1.0),
    ]


def test_center_divergence_needs_new_extreme_and_smaller_area():
    moves = _center_moves(leaving_end=12, leaving_area=4.0)
    ranged = center_divergence(moves, [_center()], "range")
    trend = center_divergence(moves, [_center()], "down")
    assert ranged.startswith("盘整背驰候选")
    assert trend.startswith("趋势背驰候选")
    assert "未经线段和次级别确认" in trend


def test_center_divergence_rejects_weak_cases():
    no_new_low = center_divergence(_center_moves(18, 4.0), [_center()], "range")
    bigger_area = center_divergence(_center_moves(12, 12.0), [_center()], "range")
    still_inside = center_divergence(_center_moves(12, 4.0), [_center("延伸")], "range")
    assert "不构成背驰" in no_new_low
    assert "没有缩小" in bigger_area
    assert "还没有离开" in still_inside


def test_exceeded_last_stroke_is_marked_extending():
    top = StructureBar("t", 21, 21, 20, 21)
    merged = [top, StructureBar("a", 21.5, 22, 21, 21.5), StructureBar("b", 23, 24, 22, 23)]
    pending = _pending_stroke([("top", 0, top)], merged, merged)
    assert pending["extending"]
    note = center_divergence(_center_moves(12, 4.0), [_center()], "range", last_extending=True)
    assert "仍在延伸" in note


def test_center_background_reads_two_centers():
    assert center_background([])[0] == "none"
    assert center_background([{"lower": 10, "upper": 12}])[0] == "range"
    assert center_background([{"lower": 10, "upper": 12}, {"lower": 14, "upper": 16}])[0] == "up"
    assert center_background([{"lower": 14, "upper": 16}, {"lower": 10, "upper": 12}])[0] == "down"


def test_overlapping_swings_extend_one_center():
    centers = chanlun_zhongshu(_strokes_from_endpoints([10, 20, 12, 18, 13, 17, 14]))
    assert len(centers) == 1
    assert centers[0]["lower"] == 12
    assert centers[0]["upper"] == 18
    assert centers[0]["state"] == "延伸"
    assert centers[0]["kind"] == "三笔重叠近似"


def test_departure_does_not_invent_a_nested_center():
    centers = chanlun_zhongshu(_strokes_from_endpoints([10, 20, 12, 18, 13, 17, 14, 19, 26]))
    assert len(centers) == 1
    assert centers[0]["state"] == "离开"
    assert (centers[0]["lower"], centers[0]["upper"]) == (12, 18)


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
    assert "周期对照" in text
    for label in ("周线", "日线", "30分钟", "5分钟"):
        assert text.count(label) >= 3
    assert "候选" in text
    assert "没有跨周期对应" in text


def test_cross_period_compares_adjacent_directions():
    spans = [
        (5, 6), (4, 5), (3, 4),
        (4, 5), (6, 7), (8, 9), (10, 11), (12, 14),
        (10, 11), (8, 9), (6, 7), (5, 6), (4.5, 5.5),
        (6, 7), (8, 9), (10, 11), (11, 13),
        (9, 10), (7, 8), (6, 7), (5.5, 6.5),
        (7, 8), (9, 10), (11, 12), (13, 15),
    ]
    bars = [_bar(index, low, high) for index, (low, high) in enumerate(spans, start=1)]
    good = {"source": "x", "error": "", "chanlun": analyze_chanlun_bars(bars), "elliott": analyze_elliott_bars(bars, 0.05)}
    text = render_cross_period({
        "week": good,
        "day": good,
        "30m": {"source": "", "error": "", "chanlun": {"status": "unavailable"}, "elliott": {"status": "unavailable"}},
    })
    assert "不是级别联立" in text
    assert "周线与日线同向" in text
    assert "30分钟: 没有可用结构" in text


def test_historical_request_drops_future_bars_on_every_period(monkeypatch):
    calls = []

    class FakeManager:
        def read_historical_bars(self, symbol, start, end, period):
            import pandas as pd
            items, source = self.get_kline_with_fallback(symbol, "day", PERIOD_LIMITS["day"], "qfq", start, end)
            return pd.DataFrame(items), source

        def get_kline_with_fallback(self, code, period="day", limit=120, adj=None, start_date=None, end_date=None):
            calls.append({
                "period": period,
                "start_date": start_date,
                "end_date": end_date,
                "limit": limit,
            })
            return ([{
                "time": "2026-10-19 10:30:00",
                "open": 10,
                "high": 11,
                "low": 9,
                "close": 10,
            }], "fake")

    monkeypatch.setattr("app.services.data_sources.manager.get_app_data_source_manager", lambda: FakeManager())
    clear_structure_cache()
    result = analyze_price_structure(
        "000001",
        end_date="2024-01-31",
        periods=("week", "day", "30m", "5m"),
    )
    text = render_price_structure("000001", result)
    seen = {item["period"]: item for item in calls}
    for period in ("week", "day", "30m", "5m"):
        assert seen[period]["end_date"] == "2024-01-31"
        assert seen[period]["start_date"]
        assert not seen[period]["start_date"].startswith("1990")
        assert seen[period]["limit"] == PERIOD_LIMITS[period]
        assert result[period]["chanlun"]["status"] == "unavailable"
    assert "2026-10-19" not in text
    assert "没有可用行情" in text


def test_structure_result_is_reused_within_the_cache_window(monkeypatch):
    calls = []

    def fake_load(symbol, period, end_date=None):
        calls.append(period)
        bars = [_bar(index, 10, 12) for index in range(1, 25)]
        return bars, "fixture", "ok"

    monkeypatch.setattr(
        "core.tools.implementations.technical.price_structure.load_structure_bars",
        fake_load,
    )
    clear_structure_cache()
    analyze_price_structure("600519", end_date="2024-01-31", periods=("day",))
    analyze_price_structure("600519", end_date="2024-01-31", periods=("day",))
    assert calls == ["day"]
