"""Silence detection across all audio tracks of a video file."""

import tempfile
from pathlib import Path
from typing import List

from pydub import AudioSegment
from pydub.silence import detect_silence

from . import ffprobe
from .segments import Segment, intersect_many, merge_segments, shrink_segments


class SilenceDetector:
    def __init__(
        self,
        silence_threshold: float = -40.0,
        min_silence_duration: float = 0.5,
        padding: float = 0.1,
    ):
        """
        Args:
            silence_threshold: audio level in dBFS below which is silence
            min_silence_duration: shortest silence worth removing, in seconds
            padding: buffer kept around speech; shrinks each detected
                silence on both ends so cuts don't clip words
        """
        self.silence_threshold = silence_threshold
        self.min_silence_duration = min_silence_duration
        self.padding = padding

    def detect(self, video_path: Path, log=print) -> List[Segment]:
        """Detect spans where ALL audio tracks are silent.

        Returns merged (start, end) segments, already shrunk by `padding`
        and filtered to at least `min_silence_duration`.
        """
        num_tracks = ffprobe.get_audio_track_count(video_path)
        if num_tracks == 0:
            raise RuntimeError(
                f"'{video_path}' has no audio tracks; silence detection needs audio."
            )
        duration = ffprobe.get_duration(video_path)

        per_track = []
        for track_idx in range(num_tracks):
            log(f"  Analyzing audio track {track_idx + 1}/{num_tracks}...")
            raw = self._detect_track(video_path, track_idx, duration)
            per_track.append(raw)
            log(f"    Found {len(raw)} silence span(s)")

        common = intersect_many(per_track)
        common = shrink_segments(common, self.padding)
        return [
            (start, end) for start, end in common
            if end - start >= self.min_silence_duration
        ]

    def _detect_track(
        self,
        video_path: Path,
        track_index: int,
        total_duration: float,
        chunk_seconds: float = 300.0,
    ) -> List[Segment]:
        """Detect raw silence spans in one track, processing audio in chunks.

        Chunks overlap by more than min_silence_duration so a silence spanning
        a chunk boundary is fully seen by at least one chunk; overlapping
        detections are merged afterwards.
        """
        min_silence_ms = int(self.min_silence_duration * 1000)
        overlap = self.min_silence_duration + 0.5
        step = chunk_seconds - overlap

        raw: List[Segment] = []
        chunk_start = 0.0
        while chunk_start < total_duration:
            chunk_duration = min(chunk_seconds, total_duration - chunk_start)
            try:
                with tempfile.NamedTemporaryFile(suffix=".wav") as tmp:
                    ffprobe.extract_audio_wav(
                        video_path, Path(tmp.name), track_index,
                        start=chunk_start, duration=chunk_duration,
                    )
                    audio = AudioSegment.from_wav(tmp.name)
                    ranges = detect_silence(
                        audio,
                        min_silence_len=min_silence_ms,
                        silence_thresh=int(self.silence_threshold),
                    )
                for start_ms, end_ms in ranges:
                    raw.append(
                        (chunk_start + start_ms / 1000.0, chunk_start + end_ms / 1000.0)
                    )
            except Exception as e:
                print(f"    Warning: failed to process chunk at {chunk_start:.1f}s: {e}")

            chunk_start += step

        return merge_segments(raw)
