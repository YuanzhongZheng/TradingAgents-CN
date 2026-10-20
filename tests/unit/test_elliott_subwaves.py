import pytest

from core.tools.implementations.technical.elliott_subwaves import validate_subwaves
from core.tools.implementations.technical.elliott_wave import analyze_elliott_bars, render_elliott
from core.tools.implementations.technical.structure_bars import StructureBar


# Real OHLC input, no mocked pivots: outer reversals are 10%; inner reversals 3.33%.
# Parent 1/3/5 form impulses, 2/4 form threes. Each internal pivot is visible.
IMPULSE_LEGS = [
    [100, 110, 105, 122, 116, 130],
    [130, 120, 125, 115],
    [115, 135, 125, 155, 145, 170],
    [170, 155, 163, 145],
    [145, 160, 151, 176, 165, 190],
]


def fixture(legs, reverse=False):
    bars, endpoints = [], []
    for n, leg in enumerate(legs):
        for i, value in enumerate(leg):
            if n and i == 0:
                continue
            value = 300 - value if reverse else value
            bars.append(StructureBar(f'T{len(bars):03}', value, value, value, value))
            if n == 0 and i == 0:
                endpoints.append(bars[-1])
        endpoints.append(bars[-1])
    first = 'high' if reverse else 'low'
    other = 'low' if reverse else 'high'
    points = [(first if i % 2 == 0 else other, b) for i, b in enumerate(endpoints)]
    return bars, points


@pytest.mark.parametrize('reverse', [False, True])
def test_real_nested_impulse_passes_53535_and_price_rules(reverse):
    bars, points = fixture(IMPULSE_LEGS, reverse)
    # Price reflection changes percentage denominators, so use a threshold that
    # resolves the same swings at the higher reflected starting price.
    result = validate_subwaves(bars, points, 'impulse', .05 if reverse else .1)
    assert result['status'] == 'passed', result
    assert [x['observed'] for x in result['legs']] == [5, 3, 5, 3, 5]
    assert all(all(ok for _, ok in leg['checks']) for leg in result['legs'])


def test_matching_wave_count_is_not_enough_when_inner_wave_four_overlaps():
    legs = list(IMPULSE_LEGS)
    legs[2] = [115, 135, 125, 155, 132, 170]
    bars, points = fixture(legs)
    result = validate_subwaves(bars, points, 'impulse', .1)
    assert result['status'] == 'failed'
    third = result['legs'][2]
    assert third['observed'] == 5
    assert ('第4浪没有进入第1浪价格区间', False) in third['checks']


def test_insufficient_resolution_does_not_pass_by_inventing_subwaves():
    bars, points = fixture([[100, 130], [130, 115], [115, 170], [170, 145], [145, 190]])
    result = validate_subwaves(bars, points, 'impulse', .1)
    assert result['status'] == 'insufficient'
    assert not any(x['status'] == 'passed' for x in result['legs'])


def test_future_bars_do_not_change_internal_partition():
    bars, points = fixture(IMPULSE_LEGS)
    result = validate_subwaves(bars, points, 'impulse', .1)
    bars += [StructureBar('future', 1, 999, 1, 900)]
    assert validate_subwaves(bars, points, 'impulse', .1) == result


@pytest.mark.parametrize('shape, expected, legs', [
    ('之字形', [5, 3, 5], [IMPULSE_LEGS[0], [130, 120, 125, 115], IMPULSE_LEGS[2]]),
    ('平台形', [3, 3, 5], [[100, 120, 110, 130], [130, 110, 120, 102], [102, 120, 110, 135, 125, 150]]),
])
def test_corrections_have_distinct_internal_templates(shape, expected, legs):
    bars, points = fixture(legs)
    result = validate_subwaves(bars, points, 'abc', .1, shape)
    assert result['status'] == 'passed', result
    assert [x['observed'] for x in result['legs']] == expected


def test_unknown_correction_shape_not_silently_treated_as_zigzag():
    bars, points = fixture(IMPULSE_LEGS[:3])
    assert validate_subwaves(bars, points, 'abc', .1, '未细分')['status'] == 'unsupported'


def test_report_and_parent_label_include_validation_result():
    # Lower inner corrections stay below the parent's 10% reversal threshold.
    bars, _ = fixture(IMPULSE_LEGS)
    bars += [StructureBar('T999', 150, 150, 150, 150)]
    result = analyze_elliott_bars(bars, .1)
    assert result['subwave_validation']['legs']
    assert '一层子浪检验' in result['core_pattern']
    rendered = render_elliott('日线', result)
    assert '内部子浪检验' in rendered and '父浪1' in rendered
    assert '未检验内部子浪' not in rendered
    assert '未递归检验更深子浪' in rendered


def test_full_pipeline_does_not_hide_failed_internal_rules():
    legs = list(IMPULSE_LEGS)
    legs[2] = [115, 130, 125, 137, 129, 170]
    bars, _ = fixture(legs)
    bars += [StructureBar('T999', 150, 150, 150, 150)]
    result = analyze_elliott_bars(bars, .1)
    assert result['subwave_validation']['status'] == 'failed'
    assert '一层子浪检验：未通过' in result['core_pattern']
    assert '未通过规则:' in render_elliott('日线', result)


def test_extra_internal_waves_are_not_compressed_to_force_a_pass():
    legs = list(IMPULSE_LEGS)
    legs[2] = [115, 135, 125, 145, 135, 155, 145, 170]
    bars, points = fixture(legs)
    result = validate_subwaves(bars, points, 'impulse', .1)
    assert result['status'] == 'unsupported'
    assert result['legs'][2]['observed'] == 7


def test_following_abc_gets_its_own_internal_validation():
    bars, _ = fixture(IMPULSE_LEGS)
    # Confirm a complete ABC after the five parent waves, then confirm C.
    for value in [160, 180, 150, 175]:
        bars.append(StructureBar(f'T{len(bars):03}', value, value, value, value))
    result = analyze_elliott_bars(bars, .1)
    following = result['following_subwave_validation']
    assert following is not None
    assert following['expected'] == [5, 3, 5]
    assert following['status'] == 'insufficient'
    assert '后续ABC子浪检验：分辨率不足' in result['pattern']
