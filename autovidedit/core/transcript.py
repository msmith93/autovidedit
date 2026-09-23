"""Human- and agent-readable views of an edit plan.

`format_transcript` is the compact text form an AI (or a person) reads to
decide what to cut; every line starts with the entry id that `plan set` and
`plan apply` take.
"""

from typing import List, Optional

from .edit_plan import EditPlan, Entry


def fmt_time(seconds: float) -> str:
    """0:05.3, 12:04.0, 1:02:03.5"""
    seconds = max(0.0, seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours >= 1:
        return f"{int(hours)}:{int(minutes):02d}:{secs:04.1f}"
    return f"{int(minutes)}:{secs:04.1f}"


def listed_entries(plan: EditPlan, start: Optional[float] = None,
                   end: Optional[float] = None) -> List[Entry]:
    """Sentences, gaps and cuts overlapping [start, end], in time order.

    Silences are omitted: they're an analyzer detail folded into pauses.
    When a plan has no sentences (silence-only mode) silences are listed.
    """
    kinds = {"sentence", "gap", "cut"} if plan.sentences else {"silence", "cut"}
    lo = start if start is not None else float("-inf")
    hi = end if end is not None else float("inf")
    return sorted(
        (e for e in plan.entries if e.kind in kinds and e.end > lo and e.start < hi),
        key=lambda e: (e.start, e.end),
    )


def format_entry(entry: Entry) -> str:
    decision = "KEEP  " if entry.decision == "keep" else "REMOVE"
    span = f"{fmt_time(entry.start)}-{fmt_time(entry.end)}"
    if entry.kind == "sentence":
        body = f"T{entry.track or 1}  {entry.text or ''}"
    elif entry.kind == "gap":
        body = f"(gap {entry.end - entry.start:.1f}s, no speech)"
    elif entry.kind == "silence":
        body = f"(silence {entry.end - entry.start:.1f}s)"
    else:
        body = f"(cut {entry.end - entry.start:.1f}s)"
    line = f"[{entry.id}] {span}  {decision}  {body}"
    if entry.source != "analyzer" or entry.rationale:
        note = entry.source
        if entry.rationale:
            note += f": {entry.rationale}"
        line += f"  <{note}>"
    return line


def format_transcript(plan: EditPlan, start: Optional[float] = None,
                      end: Optional[float] = None) -> str:
    return "\n".join(format_entry(e) for e in listed_entries(plan, start, end))


def transcript_json(plan: EditPlan, start: Optional[float] = None,
                    end: Optional[float] = None, include_words: bool = False) -> List[dict]:
    exclude = None if include_words else {"words"}
    return [
        e.model_dump(exclude=exclude, exclude_none=True)
        for e in listed_entries(plan, start, end)
    ]
