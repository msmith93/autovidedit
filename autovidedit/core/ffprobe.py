"""Thin subprocess wrappers around ffmpeg/ffprobe."""

import json
import subprocess
import threading
import time
from functools import lru_cache
from pathlib import Path


class FFmpegError(RuntimeError):
    pass


class RenderCancelled(Exception):
    """Raised when an ffmpeg run is aborted via its cancel_event."""


def run_ffmpeg(
    args: list,
    timeout: float = None,
    cancel_event: "threading.Event | None" = None,
) -> subprocess.CompletedProcess:
    """Run ffmpeg with the given arguments, raising FFmpegError on failure.

    If ``cancel_event`` is provided the process is polled while it runs; when
    the event is set the process is terminated (then killed if it lingers) and
    ``RenderCancelled`` is raised.
    """
    cmd = ["ffmpeg", "-hide_banner", "-y"] + [str(a) for a in args]

    if cancel_event is None:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            raise FFmpegError(
                f"ffmpeg failed: {' '.join(cmd)}\n{result.stderr.strip()[-2000:]}"
            )
        return result

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + timeout if timeout else None
    while True:
        if cancel_event.is_set():
            proc.terminate()
            try:
                proc.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
            raise RenderCancelled("Render cancelled")
        try:
            stdout, stderr = proc.communicate(timeout=0.2)
        except subprocess.TimeoutExpired:
            if deadline and time.monotonic() > deadline:
                proc.kill()
                proc.communicate()
                raise subprocess.TimeoutExpired(cmd, timeout)
            continue
        if proc.returncode != 0:
            raise FFmpegError(
                f"ffmpeg failed: {' '.join(cmd)}\n{(stderr or '').strip()[-2000:]}"
            )
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def probe(video_path: Path) -> dict:
    """Probe a media file, returning ffprobe's JSON output as a dict."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_format", "-show_streams",
        "-of", "json", str(video_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise FFmpegError(f"ffprobe failed for {video_path}:\n{result.stderr.strip()}")
    return json.loads(result.stdout)


def check_ffmpeg_installed():
    """Raise FFmpegError if ffmpeg is not available on PATH."""
    try:
        subprocess.run(
            ["ffmpeg", "-version"], capture_output=True, check=True
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        raise FFmpegError(
            "FFmpeg is not installed or not available in PATH. "
            "Install it with: sudo apt install ffmpeg"
        )


def get_audio_track_count(video_path: Path) -> int:
    """Return the number of audio streams in the file."""
    info = probe(video_path)
    return sum(1 for s in info.get("streams", []) if s.get("codec_type") == "audio")


def get_duration(video_path: Path) -> float:
    """Return media duration in seconds, trying format, streams, then packets."""
    info = probe(video_path)

    candidates = [info.get("format", {}).get("duration")]
    for stream in info.get("streams", []):
        candidates.append(stream.get("duration"))
    for value in candidates:
        try:
            duration = float(value)
            if duration > 0:
                return duration
        except (TypeError, ValueError):
            continue

    # Fall back to the last video packet timestamp
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "packet=pts_time", "-of", "csv=p=0", str(video_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode == 0 and result.stdout.strip():
        timestamps = [float(t) for t in result.stdout.split() if t]
        if timestamps:
            return max(timestamps) + 0.1

    raise FFmpegError(
        f"Could not determine duration of {video_path}. "
        "The file may be incomplete or corrupted."
    )


def get_frame_rate(video_path: Path, default: int = 30) -> int:
    """Return the rounded video frame rate, or `default` if undetectable."""
    info = probe(video_path)
    video_stream = next(
        (s for s in info.get("streams", []) if s.get("codec_type") == "video"), None
    )
    if not video_stream:
        return default
    rate = video_stream.get("r_frame_rate") or video_stream.get("avg_frame_rate")
    try:
        num, den = map(int, rate.split("/"))
        return round(num / den) if den > 0 else default
    except (AttributeError, ValueError, ZeroDivisionError):
        return default


@lru_cache(maxsize=1)
def has_nvenc() -> bool:
    """Check if the NVIDIA NVENC h264 encoder is available."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0 and "h264_nvenc" in result.stdout
    except Exception:
        return False


def extract_audio_wav(
    video_path: Path,
    output_path: Path,
    track_index: int = 0,
    sample_rate: int = 16000,
    start: float = None,
    duration: float = None,
):
    """Extract one audio track to 16-bit mono WAV (optionally a time slice)."""
    args = []
    if start is not None:
        args += ["-ss", f"{start:.3f}"]
    if duration is not None:
        args += ["-t", f"{duration:.3f}"]
    args += [
        "-i", str(video_path),
        "-map", f"0:a:{track_index}",
        "-acodec", "pcm_s16le", "-ar", str(sample_rate), "-ac", "1",
        str(output_path),
    ]
    run_ffmpeg(args)
