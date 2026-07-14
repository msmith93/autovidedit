import pytest

from autovidedit.core.ffprobe import get_duration
from autovidedit.core.silence import SilenceDetector
from tests.conftest import FIXTURE_DURATION, SILENCE_SPANS

TOLERANCE = 0.25  # AAC framing + pydub ms resolution


def assert_spans_match(found, expected, padding):
    assert len(found) == len(expected), f"expected {expected}, found {found}"
    for (f_start, f_end), (e_start, e_end) in zip(found, expected):
        assert abs(f_start - (e_start + padding)) < TOLERANCE
        assert abs(f_end - (e_end - padding)) < TOLERANCE


def test_detects_known_silences(fixture_video):
    detector = SilenceDetector(padding=0.1)
    found = detector.detect(fixture_video, log=lambda *_: None)
    assert_spans_match(found, SILENCE_SPANS, padding=0.1)


def test_silence_spanning_chunk_boundary(fixture_video):
    # chunk_seconds=5.0 puts a chunk boundary at 5s, inside the 3-6s silence
    detector = SilenceDetector(padding=0.0)
    duration = get_duration(fixture_video)
    raw = detector._detect_track(fixture_video, 0, duration, chunk_seconds=5.0)

    spanning = [s for s in raw if s[0] < 5.0 < s[1]]
    assert spanning, f"silence crossing the 5s chunk boundary was split or missed: {raw}"
    start, end = spanning[0]
    assert abs(start - 3.0) < TOLERANCE
    assert abs(end - 6.0) < TOLERANCE


def test_fixture_duration_sanity(fixture_video):
    assert abs(get_duration(fixture_video) - FIXTURE_DURATION) < 0.5
