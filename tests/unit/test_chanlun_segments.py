from core.tools.implementations.technical.chanlun_segments import chanlun_segments, standard_features
from core.tools.implementations.technical.structure_bars import StructureBar


def strokes(prices, down=False):
    if down:
        prices = [100 - p for p in prices]
    return [(('top' if down else 'bottom') if i % 2 == 0 else ('bottom' if down else 'top'),
             i * 5, StructureBar(f'T{i:02}', p, p, p, p)) for i, p in enumerate(prices)]


def test_no_gap_requires_right_feature_and_three_overlapping_strokes():
    s = strokes([10, 20, 15, 25, 18, 22, 14])
    assert not chanlun_segments(s[:-1], last_endpoint_confirmed=True)['segments']
    result = chanlun_segments(s, last_endpoint_confirmed=True)
    segment = result['segments'][0]
    assert (segment['start_stroke'], segment['end_stroke'], segment['confirmed_stroke']) == (0, 3, 6)
    assert not segment['feature_gap']
    assert segment['end'] == 'T03' and segment['confirmed_at'] == 'T06'


def test_downward_segment_is_symmetric():
    s = strokes([10, 20, 15, 25, 18, 22, 14], down=True)
    segment = chanlun_segments(s, last_endpoint_confirmed=True)['segments'][0]
    assert segment['direction'] == 'down'
    assert segment['end_stroke'] == 3


def test_gap_waits_for_reverse_feature_fractal_not_just_three_strokes():
    s = strokes([10, 20, 15, 30, 25, 28, 18, 24, 20, 27])
    waiting = chanlun_segments(s[:7], last_endpoint_confirmed=True)
    assert not waiting['segments']
    assert waiting['candidate']['feature_gap']
    assert not chanlun_segments(s[:9], last_endpoint_confirmed=True)['segments']
    segment = chanlun_segments(s, last_endpoint_confirmed=True)['segments'][0]
    assert segment['feature_gap']
    assert segment['end_stroke'] == 3 and segment['confirmed_stroke'] == 9


def test_new_extreme_invalidates_unconfirmed_gap_endpoint():
    s = strokes([10, 20, 15, 30, 25, 28, 18, 31, 20, 33])
    result = chanlun_segments(s, last_endpoint_confirmed=True)
    assert not any(x['end_stroke'] == 3 for x in result['segments'])
    assert result['candidate']['end_stroke'] != 3


def test_containment_retains_real_extreme_indices():
    s = strokes([0, 20, 10, 18, 12, 25, 15, 22, 14])
    f = standard_features(s, 0)
    assert (f[0].low, f[0].high, f[0].low_index, f[0].high_index) == (12, 20, 4, 1)
    assert chanlun_segments(s, last_endpoint_confirmed=True)['segments'][0]['end_stroke'] == 5


def test_mutable_last_stroke_cannot_confirm_segment():
    s = strokes([10, 20, 15, 25, 18, 22, 14, 19])
    assert not chanlun_segments(s[:7])['segments']
    segment = chanlun_segments(s)['segments'][0]
    assert segment['confirmed_at'] == 'T07'


def test_confirmed_segments_do_not_repaint_with_future_strokes():
    s = strokes([10, 20, 15, 25, 18, 22, 14, 19, 16, 23, 18, 21, 15, 20, 17, 24])
    full = chanlun_segments(s, last_endpoint_confirmed=True)['segments']
    assert len(full) >= 3
    for stop in range(4, len(s) + 1):
        prefix = chanlun_segments(s[:stop], last_endpoint_confirmed=True)['segments']
        assert prefix == full[:len(prefix)]
        assert all(x['confirmed_stroke'] < stop for x in prefix)
    assert all(a['end_stroke'] == b['start_stroke'] for a, b in zip(full, full[1:]))
    assert all(x['stroke_count'] >= 3 for x in full)


def test_full_bar_pipeline_exposes_segments_and_segment_centers():
    from core.tools.implementations.technical.chanlun import analyze_chanlun_bars, render_chanlun
    anchors = [12, 10, 20, 15, 25, 18, 22, 14, 19, 16, 23, 18, 21, 15, 20, 17, 24, 19]
    prices = [a + (b - a) * k / 5 for a, b in zip(anchors, anchors[1:]) for k in range(5)] + [anchors[-1]]
    bars = [StructureBar(f'B{i:03}', p, p + .05, p - .05, p) for i, p in enumerate(prices)]
    result = analyze_chanlun_bars(bars)
    assert len(result['segments']) >= 3
    assert result['segment_centers']
    assert result['segment_centers'][0]['kind'] == '三线段重叠'
    assert result['segment_candidate'] not in result['segments']
    for segment in result['segments']:
        idx = segment['confirmed_bar_index']
        assert segment['confirmed_at'] == bars[idx].time
        assert segment['end'] < segment['confirmed_at']
        prefix = analyze_chanlun_bars(bars[:idx + 1])
        assert segment in prefix['segments']
    text = render_chanlun('日线', result)
    assert '确认于' in text and '三线段重叠区间:' in text


def test_downward_gap_needs_the_second_feature_fractal_too():
    s = strokes([10, 20, 15, 30, 25, 28, 18, 24, 20, 27], down=True)
    assert not chanlun_segments(s[:7], last_endpoint_confirmed=True)['segments']
    segment = chanlun_segments(s, last_endpoint_confirmed=True)['segments'][0]
    assert segment['direction'] == 'down' and segment['feature_gap']
    assert segment['confirmed_stroke'] == 9
