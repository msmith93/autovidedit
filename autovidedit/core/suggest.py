"""AI editing pass: Claude reads the transcript and looks at frames, then proposes cuts.

The timeline is split into windows (default 10 minutes). For each window
Claude gets the id-tagged transcript with word timings, one minute of
read-only context on each side, and still frames sampled across the window.
It returns a DecisionSet (the same format `autovidedit plan apply` takes),
which the caller merges with `EditPlan.apply_decisions` so human decisions
are never overwritten.

Requires the optional `anthropic` dependency: pip install -e .[ai]
"""

import base64
import json
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from . import ffprobe
from .edit_plan import CutItem, DecisionItem, DecisionSet, EditPlan, Entry
from .frames import frame_at, sample_times
from .project import Project
from .transcript import fmt_time, listed_entries

LogFn = Callable[[str], None]

FALLBACK_BETA = "server-side-fallback-2026-07-01"

# USD per million tokens: (input, output). Cache writes bill at 1.25x input,
# cache reads at 0.1x. Used only for the cost estimate printed after a run.
PRICES = {
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-fable-5-1": (10.0, 50.0),
    "claude-sonnet-5": (2.0, 10.0),
}

SYSTEM_PROMPT = """\
You are the editor for a recorded video (typically a screen recording or \
gameplay session with one or more people talking, each on their own audio \
track). An automatic pass has already shortened silences. Your job is the \
judgment calls a human editor would make, so the final video is tight and \
watchable without losing anything the audience needs.

You receive one window of the timeline: an id-tagged transcript and still \
frames sampled across it. Lines marked CONTEXT are outside the window; read \
them for continuity but do not return decisions for them.

Each transcript line looks like:
[id] start-end  DECISION  T<track>  text   {word timings}
Gap lines are spans where nobody speaks. Every entry already has a decision; \
KEEP means it stays in the video.

Propose changes for these situations:
1. False starts and retakes. When a speaker restarts or repeats a line, \
remove the earlier attempts and keep the last complete, fluent take.
2. Off-topic or setup chatter. Remove pre-recording setup ("is it \
recording?", "ready?"), mid-session interruptions ("sorry, one sec", \
talking to someone off-stream) and tangents that don't serve the video. \
Keep banter and reactions that are part of the entertainment; when unsure, keep.
3. Visually dead stretches. Use the frames: long loading screens, idle \
menus or unchanged screens where nothing important is being said can go. \
Remove them via their gap or sentence ids, or as extra cuts.
4. Filler words. Cut standalone "um", "uh", "er" and similar using the word \
timings, as extra_cuts spanning just the filler word. Never cut a word that \
carries meaning, and leave fillers inside fast speech alone if cutting them \
would sound choppy.

Rules:
- Return a decision only for entries whose decision should change. Use ids \
exactly as given, and only ids from inside the window.
- A removed sentence never removes overlapping speech another track keeps, so \
judge each track's lines on their own.
- extra_cuts are time spans in seconds on the original timeline, inside the \
window. Use them for parts of a sentence (filler words, a restarted phrase) or \
for stretches with no entry.
- Every change needs a short rationale a reviewer can check at a glance, and \
a confidence from 0 to 1. Be conservative: an unnecessary cut costs more \
than a missed one, because a human reviews every change.
"""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "decision": {"type": "string", "enum": ["keep", "remove"]},
                    "rationale": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["id", "decision", "rationale", "confidence"],
                "additionalProperties": False,
            },
        },
        "extra_cuts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "number"},
                    "end": {"type": "number"},
                    "rationale": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["start", "end", "rationale", "confidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["decisions", "extra_cuts"],
    "additionalProperties": False,
}


@dataclass
class SuggestOptions:
    model: str = "claude-opus-5"
    effort: str = "high"
    window_minutes: float = 10.0
    context_minutes: float = 1.0
    frame_every: float = 20.0
    frame_width: int = 640
    max_tokens: int = 64000


@dataclass
class WindowResult:
    start: float
    end: float
    decision_set: DecisionSet
    dropped: List[str] = field(default_factory=list)
    usage: Dict[str, int] = field(default_factory=dict)
    served_by: str = ""


def windows(duration: float, window_seconds: float) -> List[Tuple[float, float]]:
    """Split [0, duration] into consecutive windows; a short tail joins the last one."""
    if duration <= 0:
        return []
    bounds, start = [], 0.0
    while start < duration:
        end = min(duration, start + window_seconds)
        if duration - end < window_seconds * 0.25:
            end = duration
        bounds.append((start, end))
        start = end
    return bounds


def owned_entries(plan: EditPlan, start: float, end: float) -> List[Entry]:
    """Entries a window may decide: those that *start* inside it, so each entry
    belongs to exactly one window."""
    return [e for e in listed_entries(plan, start, end) if start <= e.start < end]


def format_window(plan: EditPlan, start: float, end: float, context: float) -> str:
    lines = []
    owned = {e.id for e in owned_entries(plan, start, end)}
    for entry in listed_entries(plan, start - context, end + context):
        decision = "KEEP  " if entry.decision == "keep" else "REMOVE"
        span = f"{entry.start:.2f}-{entry.end:.2f}"
        prefix = "" if entry.id in owned else "CONTEXT "
        if entry.kind == "sentence":
            words = " ".join(f"{w.word}@{w.start:.2f}-{w.end:.2f}" for w in entry.words or [])
            body = f"T{entry.track or 1}  {entry.text or ''}"
            if words and entry.id in owned:
                body += f"   {{{words}}}"
        elif entry.kind == "gap":
            body = f"(gap {entry.end - entry.start:.1f}s, no speech)"
        elif entry.kind == "cut":
            body = f"(cut: {entry.rationale or ''})"
        else:
            body = f"(silence {entry.end - entry.start:.1f}s)"
        note = f"  <{entry.source}: {entry.rationale}>" if entry.rationale and entry.kind != "cut" else ""
        lines.append(f"{prefix}[{entry.id}] {span}  {decision}  {body}{note}")
    return "\n".join(lines)


def build_request(project: Project, plan: EditPlan, start: float, end: float,
                  options: SuggestOptions, duration: float) -> dict:
    """The Messages API request for one window (without model plumbing)."""
    content: List[dict] = [{
        "type": "text",
        "text": (
            f"Video: {plan.video or project.video.name}, total length {fmt_time(duration)}.\n"
            f"This window: {start:.2f}s to {end:.2f}s ({fmt_time(start)}-{fmt_time(end)}).\n"
            f"Frames follow, then the transcript."
        ),
    }]
    for t in sample_times(duration, options.frame_every, start, end):
        path = frame_at(project, t, options.frame_width)
        content.append({"type": "text", "text": f"Frame at {t:.1f}s:"})
        content.append({
            "type": "image",
            "source": {
                "type": "base64", "media_type": "image/jpeg",
                "data": base64.standard_b64encode(path.read_bytes()).decode("ascii"),
            },
        })
    content.append({
        "type": "text",
        "text": "Transcript:\n" + format_window(
            plan, start, end, options.context_minutes * 60
        ),
    })
    return {
        "system": [{"type": "text", "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": content}],
        "output_config": {
            "effort": options.effort,
            "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
        },
    }


def parse_response(message, plan: EditPlan, start: float, end: float) -> WindowResult:
    """Validate Claude's answer for one window, dropping anything out of bounds."""
    if message.stop_reason == "refusal":
        details = getattr(message, "stop_details", None)
        category = getattr(details, "category", None) if details else None
        raise RuntimeError(f"Claude declined this window (category: {category})")
    if message.stop_reason == "max_tokens":
        raise RuntimeError("Response hit max_tokens before finishing")

    text = "".join(b.text for b in message.content if getattr(b, "type", "") == "text")
    raw = DecisionSet.model_validate(json.loads(text))

    owned = {e.id for e in owned_entries(plan, start, end)}
    result = WindowResult(start=start, end=end, decision_set=DecisionSet())
    for item in raw.decisions:
        if item.id in owned:
            result.decision_set.decisions.append(item)
        else:
            result.dropped.append(f"decision for unknown or out-of-window id {item.id}")
    for cut in raw.extra_cuts:
        lo, hi = max(cut.start, start), min(cut.end, end)
        if hi - lo < 0.05:
            result.dropped.append(f"cut {cut.start:.2f}-{cut.end:.2f} outside window or empty")
            continue
        result.decision_set.extra_cuts.append(
            CutItem(start=round(lo, 3), end=round(hi, 3),
                    rationale=cut.rationale, confidence=cut.confidence)
        )

    usage = getattr(message, "usage", None)
    for key in ("input_tokens", "output_tokens",
                "cache_creation_input_tokens", "cache_read_input_tokens"):
        result.usage[key] = int(getattr(usage, key, 0) or 0)
    result.served_by = getattr(message, "model", "") or ""
    return result


def estimate_cost(model: str, usage: Dict[str, int]) -> Optional[float]:
    if model not in PRICES:
        return None
    price_in, price_out = PRICES[model]
    return (
        usage.get("input_tokens", 0) * price_in
        + usage.get("cache_creation_input_tokens", 0) * price_in * 1.25
        + usage.get("cache_read_input_tokens", 0) * price_in * 0.1
        + usage.get("output_tokens", 0) * price_out
    ) / 1_000_000


def make_client():
    try:
        import anthropic
    except ImportError as e:
        raise ImportError("the 'anthropic' package is not installed") from e
    return anthropic.Anthropic()


def suggest(project: Project, plan: EditPlan, options: SuggestOptions = SuggestOptions(),
            client=None, log: LogFn = print) -> Tuple[DecisionSet, str]:
    """Run the AI pass over the whole video. Returns (decisions, report text).

    Does not modify the plan; merge the result with plan.apply_decisions().
    """
    client = client or make_client()
    duration = plan.duration or ffprobe.get_duration(project.video)
    combined = DecisionSet()
    totals: Dict[str, int] = {}
    notes: List[str] = []

    spans = windows(duration, options.window_minutes * 60)
    for index, (start, end) in enumerate(spans, 1):
        log(f"Window {index}/{len(spans)}: {fmt_time(start)}-{fmt_time(end)}")
        request = build_request(project, plan, start, end, options, duration)
        try:
            with client.beta.messages.stream(
                model=options.model,
                max_tokens=options.max_tokens,
                betas=[FALLBACK_BETA],
                fallbacks="default",
                **request,
            ) as stream:
                message = stream.get_final_message()
            result = parse_response(message, plan, start, end)
        except (RuntimeError, ValueError) as e:
            notes.append(f"window {fmt_time(start)}-{fmt_time(end)} skipped: {e}")
            log(f"  skipped: {e}")
            continue

        combined.decisions.extend(result.decision_set.decisions)
        combined.extra_cuts.extend(result.decision_set.extra_cuts)
        notes.extend(result.dropped)
        for key, value in result.usage.items():
            totals[key] = totals.get(key, 0) + value
        if result.served_by and result.served_by != options.model:
            notes.append(f"window {fmt_time(start)} served by fallback model {result.served_by}")
        log(f"  {len(result.decision_set.decisions)} decision(s), "
            f"{len(result.decision_set.extra_cuts)} extra cut(s)")

    cost = estimate_cost(options.model, totals)
    report = [
        f"AI pass: {len(combined.decisions)} decision change(s), "
        f"{len(combined.extra_cuts)} extra cut(s) across {len(spans)} window(s).",
        f"Tokens: {totals.get('input_tokens', 0)} in "
        f"(+{totals.get('cache_read_input_tokens', 0)} cached), "
        f"{totals.get('output_tokens', 0)} out"
        + (f", about ${cost:.2f}" if cost is not None else ""),
    ]
    report += [f"Note: {n}" for n in notes]
    return combined, "\n".join(report)
