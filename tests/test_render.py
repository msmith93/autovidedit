from autovidedit.core.ffprobe import get_audio_track_count, get_duration, probe
from autovidedit.core.render import VideoRenderer, render_edit_plan
from tests.conftest import FIXTURE_DURATION


def test_remove_segments_re_encode(tmp_path, fixture_video):
    output = tmp_path / "out.mkv"
    renderer = VideoRenderer()
    removed = [(3.0, 6.0), (12.0, 13.0)]

    progress_calls = []
    renderer.remove_segments(
        fixture_video, output, removed,
        progress=lambda pct, msg: progress_calls.append((pct, msg)),
        re_encode_video=True,
    )

    assert output.exists()
    expected = FIXTURE_DURATION - 4.0
    assert abs(get_duration(output) - expected) < 0.5
    assert get_audio_track_count(output) == 1
    assert progress_calls, "progress callback was never invoked"


def test_render_to_mov_kdenlive_compatible(tmp_path, fixture_video):
    output = tmp_path / "out.mov"
    render_edit_plan(fixture_video, output, [(3.0, 6.0)], re_encode_video=True)

    assert output.exists()
    assert abs(get_duration(output) - (FIXTURE_DURATION - 3.0)) < 0.5

    info = probe(output)
    audio = next(s for s in info["streams"] if s["codec_type"] == "audio")
    assert audio["codec_name"] == "aac"


def test_no_segments_copies_input(tmp_path, fixture_video):
    output = tmp_path / "copy.mkv"
    VideoRenderer().remove_segments(fixture_video, output, [])
    assert output.exists()
    assert abs(get_duration(output) - FIXTURE_DURATION) < 0.5
