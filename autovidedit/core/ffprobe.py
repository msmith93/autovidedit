"""Thin subprocess wrappers around ffmpeg/ffprobe."""

import json
import subprocess
import tempfile
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Callable


class FFmpegError(RuntimeError):
    pass


class RenderCancelled(Exception):
    """Raised when an ffmpeg run is aborted via its cancel_event."""


def run_ffmpeg(
    args: list,
    timeout: float = None,
    cancel_event: "threading.Event | None" = None,
    on_progress: "Callable[[float], None] | None" = None,
) -> subprocess.CompletedProcess:
    """Run ffmpeg with the given arguments, raising FFmpegError on failure.

    cancel_event: polled while ffmpeg runs; when set the process is terminated
        (then killed if it lingers) and RenderCancelled is raised.
    on_progress: called with seconds of output written so far, parsed from
        ffmpeg's `-progress` stream.
    """
    cmd = ["ffmpeg", "-hide_banner", "-y"]
    if on_progress is not None:
        cmd += ["-progress", "pipe:1", "-nostats"]
    cmd += [str(a) for a in args]

    if cancel_event is None and on_progress is None:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            raise FFmpegError(
                f"ffmpeg failed: {' '.join(cmd)}\n{result.stderr.strip()[-2000:]}"
            )
        return result

    # stderr goes to a temp file so a chatty ffmpeg can't fill a pipe and block.
    with tempfile.TemporaryFile("w+") as stderr_file:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=stderr_file, text=True,
            stdin=subprocess.DEVNULL,
        )
        reader = threading.Thread(
            target=_read_progress, args=(proc.stdout, on_progress), daemon=True
        )
        reader.start()
        deadline = time.monotonic() + timeout if timeout else None
        try:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    _stop(proc)
                    raise RenderCancelled("Render cancelled")
                try:
                    proc.wait(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    if deadline and time.monotonic() > deadline:
                        _stop(proc)
                        raise subprocess.TimeoutExpired(cmd, timeout)
        finally:
            reader.join(timeout=2)
        stderr_file.seek(0)
        stderr = stderr_file.read()

    if proc.returncode != 0:
        raise FFmpegError(f"ffmpeg failed: {' '.join(cmd)}\n{stderr.strip()[-2000:]}")
    return subprocess.CompletedProcess(cmd, proc.returncode, "", stderr)


def _stop(proc: subprocess.Popen):
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _read_progress(stream, on_progress):
    """Consume ffmpeg `-progress` key=value lines, reporting out_time."""
    for line in stream:
        if on_progress is None:
            continue
        key, _, value = line.strip().partition("=")
        if key == "out_time_us" and value.isdigit():
            on_progress(int(value) / 1_000_000)


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


def get_frame_rate_fraction(video_path: Path, default: str = "30/1") -> str:
    """Return the exact video frame rate as an ffmpeg rational, e.g. '30000/1001'."""
    info = probe(video_path)
    video_stream = next(
        (s for s in info.get("streams", []) if s.get("codec_type") == "video"), None
    )
    if not video_stream:
        return default
    for key in ("r_frame_rate", "avg_frame_rate"):
        rate = video_stream.get(key) or ""
        num, _, den = rate.partition("/")
        if num.isdigit() and den.isdigit() and int(num) > 0 and int(den) > 0:
            return f"{int(num)}/{int(den)}"
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
