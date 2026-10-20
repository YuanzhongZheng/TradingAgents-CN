"""Compare optimized confirmation with the frozen, pre-optimization algorithm."""
from pathlib import Path
import random
import runpy

import pytest

from core.tools.implementations.technical import chanlun_segments as implementation
from core.tools.implementations.technical.structure_bars import StructureBar

REFERENCE = runpy.run_path(str(Path(__file__).parents[1] / "fixtures/chanlun_segments_reference.py"))


def make_strokes(prices):
    return [("top" if i % 2 else "bottom", i, StructureBar(str(i), p, p, p, p))
            for i, p in enumerate(prices)]


@pytest.mark.parametrize("confirmed", [False, True])
def test_random_histories_and_every_prefix_match_reference(confirmed):
    rng = random.Random(20261020)
    cases = [
        [10, 20, 15, 30, 25, 28, 18, 24, 20, 27, 21, 30, 19, 28],
        [100 + i // 2 + (10 if i % 2 else 0) for i in range(120)],
        [10, 20, 15, 30, 25, 28, 18, 31, 20, 33],
    ]
    for _ in range(100):
        prices = [100]
        for i in range(1, rng.randint(20, 90)):
            prices.append(prices[-1] + (1 if i % 2 else -1) * rng.randint(1, 20))
        cases.append(prices)
    for prices in cases:
        for down in (False, True):
            strokes = make_strokes(prices)
            if down:
                strokes = [("bottom" if kind == "top" else "top", i,
                            StructureBar(bar.time, -bar.open, -bar.low, -bar.high, -bar.close))
                           for kind, i, bar in strokes]
            for stop in range(len(strokes) + 1):
                prefix = strokes[:stop]
                expected = REFERENCE["chanlun_segments"](prefix, last_endpoint_confirmed=confirmed)
                actual = implementation.chanlun_segments(prefix, last_endpoint_confirmed=confirmed)
                assert actual == expected, (prices, down, stop, confirmed)


def test_feature_state_matches_full_rebuild_with_equal_prices_and_containment():
    rng = random.Random(18)
    for _ in range(100):
        strokes = make_strokes([rng.randrange(15) for _ in range(60)])
        state = implementation._FeatureSequence(strokes, 0)
        for stop in range(1, len(strokes) + 1):
            state.advance(stop)
            assert state.features == implementation.standard_features(strokes[:stop], 0)
            assert list(state.turns.values()) == list(REFERENCE["_turns"](strokes[:stop], 0))


def test_long_trend_does_not_rescan_price_prefixes(monkeypatch):
    count = 2000
    strokes = make_strokes([100 + i // 2 + (10 if i % 2 else 0) for i in range(count)])
    original = implementation._price
    reads = 0

    def count_read(stroke):
        nonlocal reads
        reads += 1
        return original(stroke)

    monkeypatch.setattr(implementation, "_price", count_read)
    result = implementation.chanlun_segments(strokes, last_endpoint_confirmed=True)
    assert result["candidate"] is not None
    assert reads < count * 10
