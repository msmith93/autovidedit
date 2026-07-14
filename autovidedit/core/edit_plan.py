"""Edit-plan model: the JSON file describing proposed and reviewed edits.

The plan is a list of entries. Each entry has:
  id            stable identifier (assigned on creation or load)
  start_time    seconds
  end_time      seconds
  modification  "REMOVED" or "NONE"
  reason        "dead air" | "Sentence" | "gap"
  duration      seconds (derived)
  content       (sentences only) {"audio_track": int, "words": str}

Semantics:
  - "dead air":  silence detected by the analyzer. REMOVED by default.
  - "Sentence":  transcribed speech. NONE (kept) by default; the reviewer
                 marks unwanted sentences REMOVED.
  - "gap":       span between sentences where no track has speech. REMOVED
                 by default; the reviewer can set NONE to keep it.
"""

import json
import uuid
from pathlib import Path
from typing import Callable, List, Optional

from .segments import Segment, gaps_between, intersect_segments, merge_segments

REASON_SILENCE = "dead air"
REASON_SENTENCE = "Sentence"
REASON_GAP = "gap"

REMOVED = "REMOVED"
KEPT = "NONE"

# Padding applied around sentences when deriving gaps, so tiny inter-word
# spans don't become gap entries (matches the old UI's behavior).
GAP_PADDING = 0.2


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


class EditPlan:
    def __init__(self, entries: Optional[List[dict]] = None):
        self.entries: List[dict] = entries or []
        for entry in self.entries:
            entry.setdefault("id", _new_id())

    @classmethod
    def load(cls, path: Path) -> "EditPlan":
        with open(path) as f:
            entries = json.load(f)
        if not isinstance(entries, list):
            raise ValueError(f"Edit plan {path} must contain a JSON list")
        return cls(entries)

    def save(self, path: Path):
        self.entries.sort(key=lambda e: float(e.get("start_time", 0.0)))
        with open(path, "w") as f:
            json.dump(self.entries, f, indent=2)

    def _add(self, start: float, end: float, reason: str, modification: str, **extra) -> dict:
        entry = {
            "id": _new_id(),
            "start_time": round(start, 3),
            "end_time": round(end, 3),
            "modification": modification,
            "reason": reason,
            **extra,
            "duration": round(end - start, 3),
        }
        self.entries.append(entry)
        return entry

    def add_silence(self, start: float, end: float) -> dict:
        return self._add(start, end, REASON_SILENCE, REMOVED)

    def add_sentence(self, start: float, end: float, words: str, audio_track: int) -> dict:
        return self._add(
            start, end, REASON_SENTENCE, KEPT,
            content={"audio_track": audio_track, "words": words},
        )

    def add_gap(self, start: float, end: float) -> dict:
        return self._add(start, end, REASON_GAP, REMOVED)

    def _by_reason(self, reason: str) -> List[dict]:
        return [e for e in self.entries if e.get("reason") == reason]

    @property
    def sentences(self) -> List[dict]:
        return self._by_reason(REASON_SENTENCE)

    @property
    def silences(self) -> List[dict]:
        return self._by_reason(REASON_SILENCE)

    @property
    def gaps(self) -> List[dict]:
        return self._by_reason(REASON_GAP)

    def get(self, entry_id: str) -> Optional[dict]:
        return next((e for e in self.entries if e.get("id") == entry_id), None)

    def set_modification(self, entry_id: str, modification: str) -> bool:
        entry = self.get(entry_id)
        if entry is None:
            return False
        entry["modification"] = modification
        return True

    def ensure_gaps(self, video_duration: float):
        """Derive gap entries from sentence spans if the plan has none yet.

        Gaps are the spans where no track has a sentence (each sentence grown
        by GAP_PADDING first). Existing gap entries are preserved so reviewer
        choices survive a reopen.
        """
        if self.gaps or not self.sentences:
            return
        sentence_ranges = [
            (float(s["start_time"]), float(s["end_time"])) for s in self.sentences
        ]
        for start, end in gaps_between(sentence_ranges, video_duration, GAP_PADDING):
            self.add_gap(start, end)

    def segments_to_remove(self, video_duration: float) -> List[Segment]:
        """Compute the final removal list for rendering.

        With sentences present: removed sentences + removed gaps + the parts
        of detected silences that fall inside kept sentences.
        Without sentences (silence-only plan): all REMOVED dead-air entries.
        """
        def ranges(entries):
            return [(float(e["start_time"]), float(e["end_time"])) for e in entries]

        if not self.sentences:
            return merge_segments(
                [r for r in ranges([e for e in self.silences if e["modification"] == REMOVED])]
            )

        removed = ranges([s for s in self.sentences if s["modification"] == REMOVED])
        removed += ranges([g for g in self.gaps if g["modification"] == REMOVED])

        kept_sentences = ranges([s for s in self.sentences if s["modification"] != REMOVED])
        silences = ranges(self.silences)
        removed += intersect_segments(silences, kept_sentences)

        return merge_segments([(s, min(e, video_duration)) for s, e in removed if s < video_duration])
