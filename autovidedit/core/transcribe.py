"""Transcription with faster-whisper, grouping words into pause-separated sentences."""

import tempfile
from pathlib import Path
from typing import Any, Dict, List

from . import ffprobe


class Transcriber:
    def __init__(self, model_size: str = "medium", device: str = "auto"):
        self.model_size = model_size
        self.device = device
        self._model = None

    def _load_model(self):
        if self._model is not None:
            return
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            raise RuntimeError(
                "faster-whisper is not installed. Install with: pip install faster-whisper"
            )
        print(f"Loading Whisper model '{self.model_size}' (device={self.device})...")
        self._model = WhisperModel(
            self.model_size, device=self.device, compute_type="auto"
        )

    def transcribe_all_tracks(
        self, video_path: Path, pause_threshold: float = 0.5
    ) -> List[Dict[str, Any]]:
        """Transcribe every audio track with word timestamps.

        Returns sentence dicts sorted by start time:
        {'start', 'end', 'words' (text), 'audio_track' (1-indexed)}
        """
        self._load_model()
        num_tracks = ffprobe.get_audio_track_count(video_path)

        all_sentences = []
        for track_idx in range(num_tracks):
            track_num = track_idx + 1
            print(f"Transcribing audio track {track_num}/{num_tracks} (this may take a while)...")

            with tempfile.NamedTemporaryFile(suffix=".wav") as tmp:
                ffprobe.extract_audio_wav(video_path, Path(tmp.name), track_idx)
                segments, _info = self._model.transcribe(
                    tmp.name, language="en", word_timestamps=True
                )
                words = [
                    {"start": w.start, "end": w.end, "word": w.word}
                    for segment in segments
                    for w in (segment.words or [])
                ]

            sentences = group_words_into_sentences(words, pause_threshold)
            for sentence in sentences:
                sentence["audio_track"] = track_num
            all_sentences.extend(sentences)
            print(f"  Found {len(sentences)} sentence(s) in track {track_num}")

        all_sentences.sort(key=lambda s: s["start"])
        return all_sentences


def group_words_into_sentences(
    words: List[Dict[str, Any]], pause_threshold: float = 0.5
) -> List[Dict[str, Any]]:
    """Group timestamped words into sentences split at pauses > pause_threshold."""
    sentences = []
    current: List[Dict[str, Any]] = []

    def flush():
        if current:
            sentences.append({
                "start": float(current[0]["start"]),
                "end": float(current[-1]["end"]),
                "words": " ".join(w["word"].strip() for w in current),
            })

    for word in words:
        if not word.get("word", "").strip():
            continue
        if current and float(word["start"]) - float(current[-1]["end"]) > pause_threshold:
            flush()
            current = []
        current.append(word)
    flush()

    return sentences
