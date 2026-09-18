"""Still frames from the source video, for visual review by a person or an AI.

Frames are written to `<out_dir>/frames/` named by timestamp in milliseconds
(`t_000012500.jpg` is 12.5 s) and reused if already present. Each frame is
grabbed with an input-side seek, so sampling a long video doesn't decode it
end to end.
"""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List, Optional

from . import ffprobe
from .project import Project

DEFAULT_EVERY = 20.0
DEFAULT_WIDTH = 640


def frame_path(project: Project, t: float, width: int = DEFAULT_WIDTH) -> Path:
    suffix = "" if width == DEFAULT_WIDTH else f"_w{width}"
    return project.frames_dir / f"t_{int(round(t * 1000)):09d}{suffix}.jpg"


def frame_at(project: Project, t: float, width: int = DEFAULT_WIDTH) -> Path:
    """Extract (or reuse) a single JPEG frame at time `t` seconds."""
    out = frame_path(project, t, width)
    if out.exists() and out.stat().st_size > 0:
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    ffprobe.run_ffmpeg([
        "-ss", f"{t:.3f}", "-i", str(project.video),
        "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "4",
        str(out),
    ])
    return out


def sample_times(duration: float, every: float = DEFAULT_EVERY,
                 start: float = 0.0, end: Optional[float] = None) -> List[float]:
    end = min(duration, end) if end is not None else duration
    times, t = [], max(0.0, start)
    # Stay a little short of the end so the seek lands on a real frame.
    while t < end - 0.05:
        times.append(round(t, 3))
        t += every
    return times


def extract_frames(project: Project, every: float = DEFAULT_EVERY,
                   width: int = DEFAULT_WIDTH, start: float = 0.0,
                   end: Optional[float] = None, workers: int = 4) -> List[dict]:
    """Sample one frame every `every` seconds; returns [{time, file}] in order.

    Also writes the index to `<frames_dir>/frames.json`.
    """
    duration = ffprobe.get_duration(project.video)
    times = sample_times(duration, every, start, end)
    project.frames_dir.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        paths = list(pool.map(lambda t: frame_at(project, t, width), times))
    index = [{"time": t, "file": str(p)} for t, p in zip(times, paths)]
    (project.frames_dir / "frames.json").write_text(json.dumps(index, indent=1))
    return index
