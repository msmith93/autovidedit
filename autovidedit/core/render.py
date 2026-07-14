"""Rendering: cut removal segments out of a video, preserving all audio tracks."""

import shutil
import tempfile
import threading
from pathlib import Path
from typing import Callable, List, Optional

from . import ffprobe
from .ffprobe import RenderCancelled
from .segments import Segment, invert_segments

__all__ = ["VideoRenderer", "render_edit_plan", "RenderCancelled", "MIN_KEEP_SEGMENT"]

# Keep-segments shorter than this are dropped rather than extracted; sub-frame
# slivers between two removals aren't worth a cut and often fail to extract.
MIN_KEEP_SEGMENT = 0.3

ProgressFn = Optional[Callable[[int, str], None]]


class VideoRenderer:
    def __init__(self):
        ffprobe.check_ffmpeg_installed()

    def _encoding_args(self) -> List[str]:
        """Video encoding arguments: visually lossless, GPU if available."""
        if ffprobe.has_nvenc():
            args = ["-c:v", "h264_nvenc", "-preset", "fast", "-rc", "vbr", "-cq", "18"]
        else:
            args = ["-c:v", "libx264", "-crf", "18", "-preset", "slow"]
        return args + ["-pix_fmt", "yuv420p", "-fps_mode", "cfr"]

    def remove_segments(
        self,
        input_path: Path,
        output_path: Path,
        segments_to_remove: List[Segment],
        progress: ProgressFn = None,
        re_encode_video: bool = False,
        min_keep_segment: float = MIN_KEEP_SEGMENT,
        cancel_event: "threading.Event | None" = None,
    ):
        """Remove the given segments, writing the concatenated remainder.

        Args:
            re_encode_video: re-encode for frame-precise cuts; otherwise stream
                copy (fast, but cuts snap to keyframes).
        """
        def report(pct: int, msg: str):
            if progress:
                progress(pct, msg)

        duration = ffprobe.get_duration(input_path)
        num_audio_tracks = ffprobe.get_audio_track_count(input_path)

        if not segments_to_remove:
            self.copy_video(input_path, output_path, cancel_event=cancel_event)
            return

        segments_to_keep = invert_segments(segments_to_remove, duration, min_keep_segment)
        if not segments_to_keep:
            raise ValueError("All video content would be removed. Aborting.")

        temp_dir = Path(tempfile.mkdtemp(prefix="autovidedit_segments_"))
        try:
            segment_files = []
            total = len(segments_to_keep)
            for idx, (start, end) in enumerate(segments_to_keep):
                if cancel_event is not None and cancel_event.is_set():
                    raise RenderCancelled("Render cancelled")
                report(int(idx / total * 90), f"Extracting segment {idx + 1} of {total}")
                print(f"  Segment {idx + 1}/{total}: {start:.2f}s - {end:.2f}s ({end - start:.2f}s)")
                segment_file = temp_dir / f"segment_{idx:04d}.mkv"
                self._extract_segment(
                    input_path, start, end, segment_file, num_audio_tracks,
                    re_encode_video, cancel_event=cancel_event,
                )
                segment_files.append(segment_file)

            report(90, f"Concatenating {len(segment_files)} segment(s)")
            self._concatenate(segment_files, output_path, num_audio_tracks, cancel_event=cancel_event)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _extract_segment(
        self,
        input_path: Path,
        start: float,
        end: float,
        output_path: Path,
        num_audio_tracks: int,
        re_encode_video: bool,
        cancel_event: "threading.Event | None" = None,
    ):
        duration = end - start
        if duration < 0.01:
            raise ValueError(
                f"Segment too short to extract: {duration:.3f}s at {start:.2f}s"
            )

        args = ["-i", str(input_path), "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
                "-map", "0:v"]
        if re_encode_video:
            args += self._encoding_args()
        else:
            args += ["-c:v", "copy"]
        for i in range(num_audio_tracks):
            args += ["-map", f"0:a:{i}"]
        args += ["-c:a", "copy", "-avoid_negative_ts", "make_zero", str(output_path)]

        ffprobe.run_ffmpeg(args, cancel_event=cancel_event)

        if not output_path.exists() or output_path.stat().st_size == 0:
            raise RuntimeError(f"Extracted segment is missing or empty: {output_path}")

    def _concatenate(
        self, video_files: List[Path], output_path: Path, num_audio_tracks: int,
        cancel_event: "threading.Event | None" = None,
    ):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False
        ) as concat_file:
            for video_file in video_files:
                concat_file.write(f"file '{video_file.absolute()}'\n")
            concat_path = Path(concat_file.name)

        try:
            args = ["-f", "concat", "-safe", "0", "-i", str(concat_path), "-map", "0:v"]
            for i in range(num_audio_tracks):
                args += ["-map", f"0:a:{i}"]
            args += ["-c", "copy", str(output_path)]
            ffprobe.run_ffmpeg(args, cancel_event=cancel_event)
        finally:
            concat_path.unlink(missing_ok=True)

    def convert_to_mov(
        self, input_path: Path, output_path: Path,
        cancel_event: "threading.Event | None" = None,
    ):
        """Re-encode to MOV with constant frame rate and CBR AAC for Kdenlive.

        Fixes VFR and timestamp-drift issues that cause audio crackling when
        the output is imported into an editor.
        """
        frame_rate = ffprobe.get_frame_rate(input_path)
        num_audio_tracks = ffprobe.get_audio_track_count(input_path)

        args = ["-i", str(input_path), "-map", "0:v"]
        for i in range(num_audio_tracks):
            args += ["-map", f"0:a:{i}"]
        args += self._encoding_args()
        args += ["-r", str(frame_rate), "-c:a", "aac"]
        # Set the target bitrate per output stream so every track carries the
        # same intent (a global -b:a is applied per-stream by ffmpeg anyway, but
        # being explicit avoids ambiguity across ffmpeg versions/filtergraphs).
        for i in range(num_audio_tracks):
            args += [f"-b:a:{i}", "192k"]
        args += [str(output_path)]
        ffprobe.run_ffmpeg(args, cancel_event=cancel_event)

    def copy_video(
        self, input_path: Path, output_path: Path,
        cancel_event: "threading.Event | None" = None,
    ):
        ffprobe.run_ffmpeg(
            ["-i", str(input_path), "-map", "0", "-c", "copy", str(output_path)],
            cancel_event=cancel_event,
        )

    def optimize_keyframes(
        self, input_path: Path, output_path: Path, progress: Optional[Callable[[str], None]] = None
    ):
        """Re-encode with GOP=5 so later stream-copy cuts land near keyframes."""
        if progress:
            progress("Optimizing keyframes (GOP=5)...")

        num_audio_tracks = ffprobe.get_audio_track_count(input_path)
        args = ["-i", str(input_path), "-map", "0:v"]
        args += self._encoding_args() + ["-g", "5"]
        for i in range(num_audio_tracks):
            args += ["-map", f"0:a:{i}"]
        args += ["-c:a", "copy", "-avoid_negative_ts", "make_zero", str(output_path)]
        ffprobe.run_ffmpeg(args)

        if not output_path.exists() or output_path.stat().st_size == 0:
            raise RuntimeError(f"Keyframe-optimized file is missing or empty: {output_path}")
        if progress:
            progress("Keyframe optimization complete")


def render_edit_plan(
    render_source: Path,
    output_path: Path,
    segments_to_remove: List[Segment],
    progress: ProgressFn = None,
    re_encode_video: bool = False,
    cancel_event: "threading.Event | None" = None,
):
    """Full render: cut segments, then convert to MOV if the output is .mov."""
    renderer = VideoRenderer()

    if output_path.suffix.lower() == ".mov":
        temp_output = output_path.parent / f"{output_path.stem}_temp.mkv"
        try:
            renderer.remove_segments(
                render_source, temp_output, segments_to_remove, progress,
                re_encode_video, cancel_event=cancel_event,
            )
            if progress:
                progress(95, "Converting to constant frame rate and CBR AAC audio")
            renderer.convert_to_mov(temp_output, output_path, cancel_event=cancel_event)
        finally:
            temp_output.unlink(missing_ok=True)
    else:
        renderer.remove_segments(
            render_source, output_path, segments_to_remove, progress,
            re_encode_video, cancel_event=cancel_event,
        )

    if progress:
        progress(100, "Rendering complete")
