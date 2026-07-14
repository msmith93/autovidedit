from autovidedit.core.segments import (
    gaps_between,
    intersect_many,
    intersect_segments,
    invert_segments,
    merge_segments,
    pad_segments,
    shrink_segments,
)


def test_merge_overlapping_and_touching():
    assert merge_segments([(5, 7), (1, 3), (2, 4), (7, 9)]) == [(1, 4), (5, 9)]


def test_merge_empty():
    assert merge_segments([]) == []


def test_invert_basic():
    assert invert_segments([(2, 4), (6, 8)], 10.0) == [(0.0, 2), (4, 6), (8, 10.0)]


def test_invert_drops_short_keeps():
    # 0.2s sliver between removals is dropped with min_length=0.3
    assert invert_segments([(0, 5), (5.2, 10)], 10.0, min_length=0.3) == []
    assert invert_segments([(0, 5), (5.5, 10)], 10.0, min_length=0.3) == [(5, 5.5)]


def test_invert_removal_past_duration():
    assert invert_segments([(8, 12)], 10.0) == [(0.0, 8)]


def test_intersect():
    assert intersect_segments([(0, 5)], [(3, 8)]) == [(3, 5)]
    assert intersect_segments([(0, 2)], [(3, 4)]) == []


def test_intersect_many_all_tracks_silent():
    tracks = [
        [(1, 5), (8, 10)],
        [(2, 6), (9, 12)],
        [(0, 4.5), (8.5, 11)],
    ]
    assert intersect_many(tracks) == [(2, 4.5), (9, 10)]


def test_intersect_many_one_track_never_silent():
    assert intersect_many([[(1, 5)], []]) == []


def test_pad_and_shrink():
    assert pad_segments([(1, 2), (2.3, 3)], 0.2, duration=3.1) == [(0.8, 3.1)]
    assert shrink_segments([(1, 2), (5, 5.1)], 0.1) == [(1.1, 1.9)]


def test_gaps_between():
    # Sentences at 1-2 and 5-6 in a 10s video, padding 0.2
    gaps = gaps_between([(1, 2), (5, 6)], duration=10.0, padding=0.2)
    assert gaps == [(0.0, 0.8), (2.2, 4.8), (6.2, 10.0)]


def test_gaps_between_no_segments():
    assert gaps_between([], duration=10.0) == []
