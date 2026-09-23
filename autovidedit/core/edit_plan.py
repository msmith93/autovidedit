"""Edit plan: the typed JSON document every stage reads and writes.

File format (version 2)::

    {
      "version": 2,
      "video": "recording.mkv",
      "duration": 3600.0,
      "options": {"max_pause": 0.5},
      "entries": [Entry, ...]
    }

Entry kinds:
  sentence  transcribed speech on one audio track. Kept by default.
  silence   span where every audio track is quiet. Removed by default.
  gap       span between sentences where no track has speech. Removed by default.
  cut       free-form removal span (AI extra cuts, filler words). Removed by default.

Every entry records who made its current decision (`source`) and optionally
why (`rationale`, `confidence`). Human decisions are protected from being
overwritten by automated passes; see `EditPlan.apply_decisions`.

Removal semantics live in `EditPlan.segments_to_remove`; the review UI gets
the result from the server rather than re-implementing it.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import Iterable, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from .segments import (
    Segment,
    gaps_between,
    intersect_segments,
    merge_segments,
    subtract_segments,
    total_length,
)

PLAN_VERSION = 2

Kind = Literal["sentence", "silence", "gap", "cut"]
Decision = Literal["keep", "remove"]
Source = Literal["analyzer", "ai", "human"]

KEEP: Decision = "keep"
REMOVE: Decision = "remove"

DEFAULT_DECISION = {"sentence": KEEP, "silence": REMOVE, "gap": REMOVE, "cut": REMOVE}

# Sentences are grown by this much before deriving gaps, and gaps shorter than
# MIN_GAP are not listed: pauses that short are never cut (see max_pause).
GAP_PADDING = 0.2
MIN_GAP = 0.5

# A pause-trimming cut shorter than this isn't worth making.
MIN_PAUSE_CUT = 0.2

# Hallucination guard: sentences mostly covered by detected silence, or
# short stock phrases Whisper invents on quiet audio.
HALLUCINATION_SILENCE_COVERAGE = 0.8
HALLUCINATION_PHRASES = {"you", "thank you", "thanks for watching", "thank you for watching"}
HALLUCINATION_MAX_DURATION = 1.5

_EPS = 1e-6


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _r(value: float) -> float:
    return round(float(value), 3)


class Word(BaseModel):
    start: float
    end: float
    word: str


class Entry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=_new_id)
    start: float
    end: float
    kind: Kind
    decision: Decision
    source: Source = "analyzer"
    rationale: Optional[str] = None
    confidence: Optional[float] = None
    track: Optional[int] = None
    text: Optional[str] = None
    words: Optional[List[Word]] = None

    @property
    def span(self) -> Segment:
        return (self.start, self.end)

    @property
    def removed(self) -> bool:
        return self.decision == REMOVE


class PlanOptions(BaseModel):
    model_config = ConfigDict(extra="allow")

    # Pauses longer than this are shortened to exactly this; shorter ones are
    # left alone. Half is kept on each side of the cut.
    max_pause: float = 0.5


class DecisionItem(BaseModel):
    """One decision on an existing entry, from an AI pass or `plan apply`."""

    id: str
    decision: Decision
    rationale: Optional[str] = None
    confidence: Optional[float] = None


class CutItem(BaseModel):
    """A new free-form removal span."""

    start: float
    end: float
    rationale: Optional[str] = None
    confidence: Optional[float] = None


class DecisionSet(BaseModel):
    """Exchange format for automated editing passes (see docs/AGENT_WORKFLOW.md)."""

    decisions: List[DecisionItem] = []
    extra_cuts: List[CutItem] = []


class ApplyResult(BaseModel):
    applied: int = 0
    unchanged: int = 0
    skipped_human: List[str] = []
    cuts_added: int = 0


class EditPlan(BaseModel):
    version: int = PLAN_VERSION
    video: str = ""
    duration: float = 0.0
    options: PlanOptions = Field(default_factory=PlanOptions)
    entries: List[Entry] = []

    # ---------- persistence ----------

    @classmethod
    def load(cls, path: Path) -> "EditPlan":
        data = json.loads(Path(path).read_text())
        if not isinstance(data, dict):
            raise ValueError(f"Edit plan {path} must be a JSON object")
        return cls.model_validate(data)

    def save(self, path: Path):
        """Write atomically (temp file + rename) so a crash can't corrupt the plan."""
        path = Path(path)
        self.entries.sort(key=lambda e: (e.start, e.end))
        payload = self.model_dump_json(indent=2, exclude_none=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(payload)
            # mkstemp creates 0600; keep the existing file's mode, else the umask default.
            if path.exists():
                mode = path.stat().st_mode & 0o777
            else:
                umask = os.umask(0)
                os.umask(umask)
                mode = 0o666 & ~umask
            os.chmod(tmp, mode)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    # ---------- construction ----------

    def _add(self, kind: Kind, start: float, end: float, **fields) -> Entry:
        fields.setdefault("decision", DEFAULT_DECISION[kind])
        entry = Entry(kind=kind, start=_r(start), end=_r(end), **fields)
        self.entries.append(entry)
        return entry

    def add_sentence(
        self, start: float, end: float, text: str, track: int,
        words: Optional[List[dict]] = None,
    ) -> Entry:
        word_models = None
        if words is not None:
            word_models = [
                Word(start=_r(w["start"]), end=_r(w["end"]), word=w["word"].strip())
                for w in words
            ]
        return self._add("sentence", start, end, text=text, track=track, words=word_models)

    def add_silence(self, start: float, end: float) -> Entry:
        return self._add("silence", start, end)

    def add_gap(self, start: float, end: float) -> Entry:
        return self._add("gap", start, end)

    def add_cut(
        self, start: float, end: float, source: Source = "ai",
        rationale: Optional[str] = None, confidence: Optional[float] = None,
    ) -> Entry:
        return self._add(
            "cut", start, end, source=source, rationale=rationale, confidence=confidence
        )

    # ---------- queries ----------

    def _of_kind(self, kind: Kind) -> List[Entry]:
        return [e for e in self.entries if e.kind == kind]

    @property
    def sentences(self) -> List[Entry]:
        return self._of_kind("sentence")

    @property
    def silences(self) -> List[Entry]:
        return self._of_kind("silence")

    @property
    def gaps(self) -> List[Entry]:
        return self._of_kind("gap")

    @property
    def cuts(self) -> List[Entry]:
        return self._of_kind("cut")

    def get(self, entry_id: str) -> Optional[Entry]:
        return next((e for e in self.entries if e.id == entry_id), None)

    # ---------- editing ----------

    def set_decision(
        self, entry_id: str, decision: Decision, source: Source = "human",
        rationale: Optional[str] = None,
    ) -> bool:
        entry = self.get(entry_id)
        if entry is None:
            return False
        entry.decision = decision
        entry.source = source
        if rationale is not None:
            entry.rationale = rationale
        return True

    def apply_decisions(
        self, decision_set: DecisionSet, source: Source = "ai", force: bool = False
    ) -> ApplyResult:
        """Merge an automated pass into the plan.

        Entries whose current decision came from a human are left alone
        unless `force` is set. Unknown ids raise ValueError before anything
        is changed. Extra cuts identical to an existing cut are skipped.
        """
        unknown = [d.id for d in decision_set.decisions if self.get(d.id) is None]
        if unknown:
            raise ValueError(f"Unknown entry ids: {unknown}")

        result = ApplyResult()
        for item in decision_set.decisions:
            entry = self.get(item.id)
            if entry.source == "human" and not force:
                result.skipped_human.append(entry.id)
                continue
            if entry.decision == item.decision and entry.source == source:
                result.unchanged += 1
            else:
                result.applied += 1
            entry.decision = item.decision
            entry.source = source
            entry.rationale = item.rationale
            entry.confidence = item.confidence

        existing = {(e.start, e.end) for e in self.cuts}
        for cut in decision_set.extra_cuts:
            key = (_r(cut.start), _r(cut.end))
            if cut.end <= cut.start or key in existing:
                continue
            self.add_cut(cut.start, cut.end, source=source,
                         rationale=cut.rationale, confidence=cut.confidence)
            existing.add(key)
            result.cuts_added += 1
        return result

    def ensure_gaps(self, video_duration: Optional[float] = None):
        """Derive gap entries from sentence spans if the plan has none yet.

        Gaps are spans where no track has a sentence (each sentence grown by
        GAP_PADDING first), at least MIN_GAP long. Existing gap entries are
        preserved so reviewer choices survive a reopen.
        """
        duration = video_duration or self.duration or None
        if self.gaps or not self.sentences:
            return
        spans = [s.span for s in self.sentences]
        for start, end in gaps_between(spans, duration, GAP_PADDING):
            if end - start >= MIN_GAP:
                self.add_gap(start, end)

    def flag_hallucinations(self) -> List[Entry]:
        """Mark analyzer-owned sentences that are probably Whisper hallucinations.

        A sentence is flagged if detected silence covers most of it, or if it
        is a short stock phrase Whisper tends to invent on quiet audio. Flagged
        sentences are set to remove with a rationale; they stay in the plan so
        a reviewer can restore them.
        """
        silences = [s.span for s in self.silences]
        flagged = []
        for sentence in self.sentences:
            if sentence.source != "analyzer":
                continue
            length = sentence.end - sentence.start
            if length <= 0:
                continue
            covered = total_length(intersect_segments(silences, [sentence.span]))
            normalized = re.sub(r"[^a-z ]", "", (sentence.text or "").lower()).strip()
            if covered / length >= HALLUCINATION_SILENCE_COVERAGE:
                reason = "no audio energy under this sentence; likely transcription hallucination"
            elif normalized in HALLUCINATION_PHRASES and length < HALLUCINATION_MAX_DURATION:
                reason = "stock phrase Whisper often invents on quiet audio"
            else:
                continue
            sentence.decision = REMOVE
            sentence.rationale = reason
            flagged.append(sentence)
        return flagged

    # ---------- removal computation ----------

    def segments_to_remove(self, video_duration: Optional[float] = None) -> List[Segment]:
        """Compute the final, merged list of spans to cut.

        Two kinds of removal:
          content  removed sentences and removed cut entries. Cut in full, except
                   that kept speech on another track always wins: the overlap is
                   subtracted so a removed sentence never clips kept speech.
          pauses   removed silences and removed gaps (minus kept gaps and content).
                   Each pause is shortened to `options.max_pause` rather than
                   deleted, keeping half on each side, so speech keeps its natural
                   rhythm. A pause edge that touches removed content or the start
                   or end of the video is cut flush instead.
        """
        duration = video_duration or self.duration
        if not duration:
            raise ValueError("video duration is unknown; pass video_duration")
        half = max(0.0, self.options.max_pause) / 2

        kept_speech = merge_segments([s.span for s in self.sentences if not s.removed])
        removed_speech = [s.span for s in self.sentences if s.removed]
        content = merge_segments(
            subtract_segments(removed_speech, kept_speech)
            + [c.span for c in self.cuts if c.removed]
        )

        pause_sources = [s.span for s in self.silences if s.removed]
        pause_sources += [g.span for g in self.gaps if g.removed]
        kept_gaps = [g.span for g in self.gaps if not g.removed]
        pauses = subtract_segments(subtract_segments(pause_sources, kept_gaps), content)

        def touches_content(t: float) -> bool:
            return any(s - _EPS <= t <= e + _EPS for s, e in content)

        trimmed = []
        for start, end in merge_segments(pauses):
            new_start = start if (start <= _EPS or touches_content(start)) else start + half
            new_end = end if (end >= duration - _EPS or touches_content(end)) else end - half
            if new_end - new_start >= MIN_PAUSE_CUT:
                trimmed.append((new_start, new_end))

        result = merge_segments(content + trimmed)
        return [(max(0.0, s), min(e, duration)) for s, e in result if s < duration]


def removal_stats(segments: Iterable[Segment]) -> dict:
    segments = list(segments)
    return {"count": len(segments), "seconds": round(total_length(segments), 3)}
