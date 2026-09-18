import subprocess
import threading
from fractions import Fraction

import numpy as np
import pytest

from autovidedit.core import ffprobe
from autovidedit.core.render import RenderCancelled, render_video, snap_to_frames, video_filter
from autovidedit.core.segments import invert_segments, total_length
from tests.conftest import FIXTURE_DURATION, SYNC_DURATION

FPS = 30


def streams(path, kind):
    return [s for s in ffprobe.probe(path)["streams"] if s["codec_type"] == kind]


def test_render_mp4_is_editor_friendly(tmp_path, fixture_video_2track):
    removed = [(3.0, 6.0), (12.0, 13.0)]
    output = tmp_path / "out.mp4"
    progress = []
    render_video(fixture_video_2track, output, removed,
                 progress=lambda pct, msg: progress.append(pct))

    assert ffprobe.probe(output)["format"]["format_name"].startswith("mov,mp4")
    video = streams(output, "video")[0]
    assert video["codec_name"] == "h264"
    assert video["r_frame_rate"] == video["avg_frame_rate"] == "30/1"   # constant frame rate
    audio = streams(output, "audio")
    assert len(audio) == 2 and all(a["codec_name"] == "aac" for a in audio)
    assert abs(ffprobe.get_duration(output) - (FIXTURE_DURATION - 4.0)) < 0.1
    assert progress[-1] == 100 and progress == sorted(progress)


def test_render_keeps_audio_and_video_in_sync_across_many_cuts(tmp_path, fixture_sync_video):
    # Cuts at awkward, non-frame-aligned times all through the clip.
    removed = [(0.37 + 1.3 * k, 0.37 + 1.3 * k + 0.23 + 0.05 * (k % 5)) for k in range(21)]
    output = tmp_path / "sync_out.mp4"
    render_video(fixture_sync_video, output, removed)

    keep = snap_to_frames(invert_segments(removed, SYNC_DURATION, 0.1), Fraction(FPS))
    expected = total_length(keep)
    assert abs(ffprobe.get_duration(output) - expected) < 0.1

    video_edges = _video_edges(output)
    for track in range(2):
        audio_edges = _audio_edges(output, track)
        assert len(video_edges) >= 10
        for t, rising in video_edges:
            nearest = min((abs(t - a) for a, r in audio_edges if r == rising), default=9)
            # The unedited source itself measures up to ~13ms between edges.
            assert nearest <= 0.8 / FPS, (
                f"track {track + 1}: picture edge at {t:.3f}s has no matching sound edge "
                f"(nearest {nearest:.3f}s)"
            )


def test_video_filter_expression():
    graph = video_filter([(0.0, 1.0), (2.0, 3.0)], [(1.0, 2.0)], Fraction(25))
    # Boundaries are compared half a frame (0.02s at 25fps) early.
    assert "gte(t,1.980000)*lt(t,2.980000)" in graph
    assert "1.000000*gte(T,1.980000)" in graph


def test_snap_to_frames():
    assert snap_to_frames([(0.37, 1.61)], Fraction(30)) == [(pytest.approx(11 / 30), pytest.approx(48 / 30))]
    assert snap_to_frames([(1.0, 1.01)], Fraction(30)) == []


def test_all_content_removed_raises(tmp_path, fixture_video):
    with pytest.raises(ValueError, match="All video content"):
        render_video(fixture_video, tmp_path / "x.mp4", [(0.0, FIXTURE_DURATION)])


def test_cancel_leaves_no_output(tmp_path, fixture_video_2track):
    cancel = threading.Event()
    cancel.set()
    output = tmp_path / "cancelled.mp4"
    with pytest.raises(RenderCancelled):
        render_video(fixture_video_2track, output, [(3.0, 6.0)], cancel_event=cancel)
    assert list(tmp_path.iterdir()) == []


# ---------- helpers ----------

def _edges(states, times):
    return [(times[i], bool(states[i])) for i in range(1, len(states))
            if states[i] != states[i - 1]]


def _video_edges(path):
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-vf", "scale=4:4,format=gray",
         "-f", "rawvideo", "-"],
        capture_output=True, check=True,
    ).stdout
    lum = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 16).mean(axis=1)
    return _edges(lum > 128, np.arange(len(lum)) / FPS)


def _audio_edges(path, track, window=0.005):
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-map", f"0:a:{track}",
         "-f", "s16le", "-ac", "1", "-ar", "48000", "-"],
        capture_output=True, check=True,
    ).stdout
    pcm = np.frombuffer(raw, dtype=np.int16).astype(float)
    n = int(48000 * window)
    rms = np.sqrt((pcm[: len(pcm) // n * n].reshape(-1, n) ** 2).mean(axis=1))
    return _edges(rms > 500, np.arange(len(rms)) * window)
