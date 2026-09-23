import subprocess
from pathlib import Path

import pytest

# The fixture video is 20s of testsrc video with a 440Hz tone, gated to
# digital silence at 3-6s and 12-13s. Silence detection with default settings
# (threshold -40dB, min 0.5s, padding 0.1s) should find both spans.
FIXTURE_DURATION = 20.0
SILENCE_SPANS = [(3.0, 6.0), (12.0, 13.0)]


@pytest.fixture(scope="session")
def fixture_video(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("fixtures") / "test_input.mkv"
    gate = "+".join(f"between(t,{s},{e})" for s, e in SILENCE_SPANS)
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-y",
            "-f", "lavfi", "-i", f"testsrc=size=160x120:rate=30:duration={FIXTURE_DURATION}",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={FIXTURE_DURATION}",
            "-af", f"volume=enable='{gate}':volume=0",
            "-c:v", "libx264", "-preset", "ultrafast", "-g", "30",
            "-c:a", "aac", "-shortest",
            str(path),
        ],
        check=True, capture_output=True,
    )
    return path


@pytest.fixture(scope="session")
def fixture_video_2track(tmp_path_factory) -> Path:
    """A 2-audio-track video for render / bitrate tests.

    Track 1 is pink noise (a loud, incompressible signal that lets the ABR AAC
    encoder actually reach the 192k target — a pure sine is trivially
    compressible and undershoots badly). Track 2 is a very quiet tone that the
    encoder intentionally undershoots. Same length/geometry as fixture_video so
    the two share a timeline.
    """
    path = tmp_path_factory.mktemp("fixtures") / "test_input_2track.mkv"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-y",
            "-f", "lavfi", "-i", f"testsrc=size=160x120:rate=30:duration={FIXTURE_DURATION}",
            "-f", "lavfi", "-i", f"anoisesrc=color=pink:amplitude=0.5:duration={FIXTURE_DURATION}",
            "-f", "lavfi", "-i", f"sine=frequency=220:duration={FIXTURE_DURATION}",
            "-map", "0:v", "-map", "1:a", "-map", "2:a",
            "-filter:a:1", "volume=0.05",
            "-c:v", "libx264", "-preset", "ultrafast", "-g", "30",
            "-c:a", "aac", "-b:a", "192k", "-shortest",
            str(path),
        ],
        check=True, capture_output=True,
    )
    return path


SYNC_DURATION = 30.0


@pytest.fixture(scope="session")
def fixture_sync_video(tmp_path_factory) -> Path:
    """Picture and sound switch on and off together every second.

    Video is white when a tone plays on both audio tracks and black when they
    are silent (on for [2k, 2k+1), off for [2k+1, 2k+2)). After any set of
    cuts, every luminance edge must still line up with an audio edge.
    """
    path = tmp_path_factory.mktemp("fixtures") / "sync.mkv"
    on = "lt(mod(t,2),1)"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-y",
            "-f", "lavfi", "-i", f"color=c=black:s=160x120:r=30:d={SYNC_DURATION}",
            "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={SYNC_DURATION}",
            "-f", "lavfi", "-i", f"sine=frequency=660:sample_rate=48000:duration={SYNC_DURATION}",
            "-filter_complex",
            f"[0:v]geq=lum='if(lt(mod(T\\,2)\\,1)\\,235\\,16)':cb=128:cr=128[v];"
            f"[1:a]volume=eval=frame:volume='{on}'[a1];"
            f"[2:a]volume=eval=frame:volume='{on}'[a2]",
            "-map", "[v]", "-map", "[a1]", "-map", "[a2]",
            "-c:v", "libx264", "-preset", "ultrafast", "-g", "30",
            "-c:a", "aac", "-b:a", "128k", "-ac", "1",
            str(path),
        ],
        check=True, capture_output=True,
    )
    return path
