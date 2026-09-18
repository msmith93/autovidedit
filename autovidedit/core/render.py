"""Single-pass render: cut removal spans out of a video.

Design (chosen so cost doesn't grow with the number of cuts):

  audio  Each track is decoded to raw PCM once and cut sample-exactly in
         Python as it streams (`cut_audio_track`).
  video  One `select` keeps frames inside kept spans, and one `setpts` moves
         each frame earlier by the total removed time before it. That is the
         same timeline the audio cut produces, so sync error is bounded by one
         frame at each cut and never accumulates. It also preserves timing for
         variable-frame-rate sources; the encoder then emits constant frame rate.

The filtergraph has a fixed number of filters no matter how many cuts there
are (a trim/concat graph with hundreds of branches ran at ~1x real time).

Output is editor-friendly MP4: H.264 at the source's frame rate, constant
frame rate, 1-second GOP (fast scrubbing in Kdenlive), every audio track
preserved as AAC 192k.
"""

import subprocess
import tempfile
import threading
from fractions import Fraction
from pathlib import Path
from typing import Callable, List, Optional

from . import ffprobe
from .ffprobe import RenderCancelled
from .segments import Segment, invert_segments, merge_segments, total_length

__all__ = [
    "render_video", "cut_audio_track", "video_filter", "snap_to_frames", "RenderCancelled",
    "MIN_KEEP_SEGMENT",
]

# Kept spans shorter than this are dropped: slivers between two removals
# aren't worth a cut.
MIN_KEEP_SEGMENT = 0.1

AUDIO_BITRATE = "192k"
_PCM_CHUNK = 1 << 20

ProgressFn = Optional[Callable[[int, str], None]]


def video_encoding_args() -> List[str]:
    """Visually lossless H.264, on the GPU when NVENC is available."""
    if ffprobe.has_nvenc():
        # -b:v 0 lets -cq drive quality; without it NVENC caps at its default bitrate.
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "18", "-b:v", "0"]
    return ["-c:v", "libx264", "-preset", "medium", "-crf", "18"]


def snap_to_frames(segments: List[Segment], fps: Fraction, offset: float = 0.0) -> List[Segment]:
    """Move every boundary to the nearest source frame boundary.

    Each kept span then holds a whole number of frames, so after the cuts the
    frames land exactly on the output frame grid. Audio is cut at the same
    snapped times, which keeps picture and sound locked together (unsnapped
    cuts let lip sync wander by up to ~2 frames before ffmpeg corrects it).
    """
    def snap(t: float) -> float:
        k = round((Fraction(t) - Fraction(offset)) * fps)
        return float(Fraction(offset) + k / fps)

    snapped = [(snap(s), snap(e)) for s, e in segments]
    return [(s, e) for s, e in snapped if e > s]


def video_filter(keep: List[Segment], removed: List[Segment], fps: Fraction,
                 offset: float = 0.0) -> str:
    """select + setpts expressions for frame-aligned kept spans.

    Container timestamps are often rounded (Matroska uses milliseconds), so
    boundaries are compared half a frame early: a frame at t belongs to the
    span [s, e) when s - half <= t < e - half. Each kept frame is moved
    earlier by the length of every removed span ending at or before it.
    """
    half = float(1 / fps) / 2
    select = "+".join(
        f"gte(t,{s + offset - half:.6f})*lt(t,{e + offset - half:.6f})" for s, e in keep
    )
    shift = "+".join(
        f"{e - s:.6f}*gte(T,{e + offset - half:.6f})" for s, e in removed
    ) or "0"
    return (
        f"[0:v]select='{select}',"
        f"setpts='(T-{offset:.6f}-({shift}))/TB'[vout]\n"
    )


def cut_audio_track(
    source: Path, track: int, keep: List[Segment], sample_rate: int, channels: int,
    output: Path, cancel_event: "threading.Event | None" = None,
    on_seconds: Optional[Callable[[float], None]] = None,
) -> int:
    """Decode one audio track and write only the kept samples as raw s16le PCM.

    Returns the number of sample frames written.
    """
    frame_bytes = 2 * channels
    bounds = [(round(s * sample_rate), round(e * sample_rate)) for s, e in keep]
    cmd = [
        "ffmpeg", "-v", "error", "-nostdin", "-i", str(source), "-map", f"0:a:{track}",
        "-f", "s16le", "-acodec", "pcm_s16le",
        "-ar", str(sample_rate), "-ac", str(channels), "pipe:1",
    ]
    written = 0
    pos = 0          # sample index of the start of the current chunk
    seg = 0          # first keep bound that may still intersect
    leftover = b""
    with open(output, "wb") as out, tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err)
        try:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise RenderCancelled("Render cancelled")
                data = proc.stdout.read(_PCM_CHUNK)
                if not data:
                    break
                data = leftover + data
                usable = len(data) - len(data) % frame_bytes
                data, leftover = data[:usable], data[usable:]
                n = usable // frame_bytes
                end = pos + n
                while seg < len(bounds) and bounds[seg][1] <= pos:
                    seg += 1
                i = seg
                while i < len(bounds) and bounds[i][0] < end:
                    lo = max(bounds[i][0], pos)
                    hi = min(bounds[i][1], end)
                    if hi > lo:
                        out.write(data[(lo - pos) * frame_bytes:(hi - pos) * frame_bytes])
                        written += hi - lo
                    i += 1
                pos = end
                if on_seconds:
                    on_seconds(pos / sample_rate)
            proc.wait()
        except BaseException:
            proc.kill()
            proc.wait()
            raise
        if proc.returncode != 0:
            err.seek(0)
            raise ffprobe.FFmpegError(
                f"audio decode failed for track {track}: "
                f"{err.read().decode(errors='replace')[-2000:]}"
            )
    return written


def render_video(
    source: Path,
    output: Path,
    segments_to_remove: List[Segment],
    progress: ProgressFn = None,
    cancel_event: "threading.Event | None" = None,
) -> Path:
    """Render `source` minus `segments_to_remove` to `output` (MP4).

    Writes to a hidden sibling file and renames on success, so a failed or
    cancelled render never leaves a truncated output behind.
    """
    def report(pct: int, msg: str):
        if progress:
            progress(pct, msg)

    ffprobe.check_ffmpeg_installed()
    info = ffprobe.probe(source)
    duration = ffprobe.get_duration(source)
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise ValueError(f"{source} has no video stream")
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    frame_rate = ffprobe.get_frame_rate_fraction(source)
    fps = Fraction(frame_rate)
    gop = max(1, round(fps))
    offset = float(video.get("start_time") or 0.0)

    keep = invert_segments(segments_to_remove, duration, MIN_KEEP_SEGMENT)
    keep = snap_to_frames(keep, fps, offset)
    if not keep:
        raise ValueError("All video content would be removed. Aborting.")
    kept_seconds = total_length(keep)
    # Everything not kept is removed, including slivers dropped by MIN_KEEP_SEGMENT,
    # so derive the shift list from `keep` rather than the raw removal list.
    removed = merge_segments(invert_segments(keep, duration))

    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(f".{output.stem}.partial{output.suffix}")

    # Audio decode+cut is ~10% of the work; video encode is the rest.
    audio_share = 10 if audio else 0

    with tempfile.TemporaryDirectory(prefix="autovidedit_render_") as tmp:
        tmp = Path(tmp)
        audio_inputs = []
        for t, stream in enumerate(audio):
            rate = int(stream.get("sample_rate") or 48000)
            channels = int(stream.get("channels") or 2)
            raw = tmp / f"track{t}.pcm"

            def on_seconds(sec, t=t):
                done = (t + min(1.0, sec / duration)) / len(audio)
                report(int(done * audio_share), f"Cutting audio track {t + 1}/{len(audio)}")

            cut_audio_track(source, t, keep, rate, channels, raw, cancel_event, on_seconds)
            audio_inputs.append((raw, rate, channels))

        script = tmp / "video.ffgraph"
        script.write_text(video_filter(keep, removed, fps, offset))

        args = ["-i", str(source)]
        for raw, rate, channels in audio_inputs:
            args += ["-f", "s16le", "-ar", str(rate), "-ac", str(channels), "-i", str(raw)]
        args += ["-filter_complex_script", str(script), "-map", "[vout]"]
        for t in range(len(audio_inputs)):
            args += ["-map", f"{t + 1}:a"]
        args += video_encoding_args()
        args += ["-pix_fmt", "yuv420p", "-r", frame_rate, "-fps_mode", "cfr", "-g", str(gop)]
        if audio_inputs:
            args += ["-c:a", "aac"]
            for t in range(len(audio_inputs)):
                args += [f"-b:a:{t}", AUDIO_BITRATE]
        args += ["-movflags", "+faststart", "-f", "mp4", str(partial)]

        def on_time(seconds_done: float):
            frac = min(1.0, seconds_done / kept_seconds) if kept_seconds else 0.0
            pct = audio_share + int(frac * (99 - audio_share))
            report(pct, f"Encoding {seconds_done:.0f}s of {kept_seconds:.0f}s")

        report(audio_share, f"Encoding {len(keep)} kept span(s), {kept_seconds:.0f}s")
        try:
            ffprobe.run_ffmpeg(args, cancel_event=cancel_event, on_progress=on_time)
            if not partial.exists() or partial.stat().st_size == 0:
                raise RuntimeError(f"ffmpeg produced no output: {partial}")
            partial.replace(output)
        finally:
            partial.unlink(missing_ok=True)

    report(100, "Rendering complete")
    return output
