"""Preview proxy generation: browser-friendly MP4 with all audio tracks mixed."""

from pathlib import Path

from . import ffprobe


def make_preview_proxy(input_path: Path, output_path: Path):
    """Create an h264/aac +faststart MP4 for review playback in a browser.

    Mixes all audio tracks into one (the browser plays a single track), uses
    a short GOP for responsive scrubbing, and moderate quality to keep the
    file small. The original file remains the render source.
    """
    num_tracks = ffprobe.get_audio_track_count(input_path)

    if ffprobe.has_nvenc():
        video_args = ["-c:v", "h264_nvenc", "-preset", "fast", "-rc", "vbr", "-cq", "23"]
    else:
        video_args = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]

    args = ["-i", str(input_path)]

    if num_tracks > 1:
        amix_inputs = "".join(f"[0:a:{i}]" for i in range(num_tracks))
        args += ["-filter_complex", f"{amix_inputs}amix=inputs={num_tracks}[aout]"]
        audio_map = ["-map", "0:v:0", "-map", "[aout]"]
    else:
        audio_map = ["-map", "0:v:0", "-map", "0:a:0"]

    args += audio_map + video_args + [
        "-pix_fmt", "yuv420p",
        "-g", "60",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(output_path),
    ]
    ffprobe.run_ffmpeg(args)
