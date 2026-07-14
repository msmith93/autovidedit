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
