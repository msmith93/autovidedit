"""Transcription with faster-whisper, grouping words into pause-separated sentences."""

import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import ffprobe

LogFn = Callable[[str], None]


class Transcriber:
    def __init__(self, model_size: str = "medium", device: str = "auto"):
        self.model_size = model_size
        self.device = device
        self._model = None

    def _load_model(self, log: LogFn = print):
        if self._model is not None:
            return
        from .cuda_libs import preload_cuda_libs

        preload_cuda_libs()
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            raise RuntimeError(
                "faster-whisper is not installed. Install with: pip install faster-whisper"
            )
        log(f"Loading Whisper model '{self.model_size}' (device={self.device})...")
        self._model = WhisperModel(
            self.model_size, device=self.device, compute_type="auto"
        )

    def transcribe_all_tracks(
        self,
        video_path: Path,
        pause_threshold: float = 0.5,
        log: LogFn = print,
        progress: Optional[Callable[[int, float], None]] = None,
    ) -> List[Dict[str, Any]]:
        """Transcribe every audio track with word timestamps.

        Returns sentence dicts sorted by start time:
        {'start', 'end', 'text', 'words': [{start, end, word}], 'audio_track' (1-indexed)}

        progress(track_number, fraction_done) is called as segments arrive.
        """
        self._load_model(log)
        num_tracks = ffprobe.get_audio_track_count(video_path)
        duration = ffprobe.get_duration(video_path)

        all_sentences = []
        for track_idx in range(num_tracks):
            track_num = track_idx + 1
            log(f"Transcribing audio track {track_num}/{num_tracks}...")

            with tempfile.NamedTemporaryFile(suffix=".wav") as tmp:
                ffprobe.extract_audio_wav(video_path, Path(tmp.name), track_idx)
                segments, _info = self._model.transcribe(
                    tmp.name,
                    language="en",
                    word_timestamps=True,
                    # Skip non-speech audio (fewer hallucinations, faster).
                    vad_filter=True,
                    # Stops one hallucination from seeding repeats in later windows.
                    condition_on_previous_text=False,
                )
                words = []
                last_decile = -1
                for segment in segments:
                    words.extend(
                        {"start": w.start, "end": w.end, "word": w.word}
                        for w in (segment.words or [])
                    )
                    fraction = min(1.0, segment.end / duration) if duration else 0.0
                    if progress:
                        progress(track_num, fraction)
                    decile = int(fraction * 10)
                    if decile > last_decile:
                        log(f"  track {track_num}: {decile * 10}%")
                        last_decile = decile

            sentences = group_words_into_sentences(words, pause_threshold)
            for sentence in sentences:
                sentence["audio_track"] = track_num
            all_sentences.extend(sentences)
            log(f"  Found {len(sentences)} sentence(s) in track {track_num}")

        all_sentences.sort(key=lambda s: s["start"])
        return all_sentences


def group_words_into_sentences(
    words: List[Dict[str, Any]], pause_threshold: float = 0.5
) -> List[Dict[str, Any]]:
    """Group timestamped words into sentences split at pauses > pause_threshold.

    Each sentence is {'start', 'end', 'text', 'words'}; word text is stripped.
    """
    sentences = []
    current: List[Dict[str, Any]] = []

    def flush():
        if current:
            sentences.append({
                "start": float(current[0]["start"]),
                "end": float(current[-1]["end"]),
                "text": " ".join(w["word"] for w in current),
                "words": list(current),
            })

    for word in words:
        text = word.get("word", "").strip()
        if not text:
            continue
        word = {"start": float(word["start"]), "end": float(word["end"]), "word": text}
        if current and word["start"] - current[-1]["end"] > pause_threshold:
            flush()
            current = []
        current.append(word)
    flush()

    return sentences
