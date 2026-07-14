"""Pure segment math shared across the pipeline.

A segment is a (start, end) tuple in seconds with start < end.
"""

from typing import List, Tuple

Segment = Tuple[float, float]


def merge_segments(segments: List[Segment]) -> List[Segment]:
    """Merge overlapping or touching segments into a sorted, disjoint list."""
    if not segments:
        return []
    ordered = sorted(segments, key=lambda s: s[0])
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def invert_segments(
    segments: List[Segment], duration: float, min_length: float = 0.0
) -> List[Segment]:
    """Return the complement of `segments` within [0, duration].

    Segments in the complement shorter than `min_length` are dropped.
    Input segments do not need to be merged or sorted.
    """
    kept = []
    current = 0.0
    for start, end in merge_segments(segments):
        if start > current and start - current >= min_length:
            kept.append((current, min(start, duration)))
        current = max(current, end)
        if current >= duration:
            break
    if current < duration and duration - current >= min_length:
        kept.append((current, duration))
    return kept


def intersect_segments(a: List[Segment], b: List[Segment]) -> List[Segment]:
    """Return the intersection of two segment lists."""
    result = []
    for a_start, a_end in merge_segments(a):
        for b_start, b_end in merge_segments(b):
            start = max(a_start, b_start)
            end = min(a_end, b_end)
            if start < end:
                result.append((start, end))
    return merge_segments(result)


def intersect_many(segment_lists: List[List[Segment]]) -> List[Segment]:
    """Return the intersection across all lists (e.g. silence common to all tracks)."""
    if not segment_lists:
        return []
    common = merge_segments(segment_lists[0])
    for segments in segment_lists[1:]:
        common = intersect_segments(common, segments)
        if not common:
            return []
    return common


def pad_segments(
    segments: List[Segment], padding: float, duration: float = None
) -> List[Segment]:
    """Grow each segment by `padding` on both sides, clamped to [0, duration]."""
    padded = []
    for start, end in segments:
        new_start = max(0.0, start - padding)
        new_end = end + padding
        if duration is not None:
            new_end = min(new_end, duration)
        if new_end > new_start:
            padded.append((new_start, new_end))
    return merge_segments(padded)


def shrink_segments(segments: List[Segment], padding: float) -> List[Segment]:
    """Shrink each segment by `padding` on both sides, dropping ones that vanish."""
    shrunk = []
    for start, end in segments:
        new_start = start + padding
        new_end = end - padding
        if new_end > new_start:
            shrunk.append((new_start, new_end))
    return shrunk


def gaps_between(
    segments: List[Segment], duration: float = None, padding: float = 0.0
) -> List[Segment]:
    """Return the gaps between segments (each grown by `padding` first).

    Includes the leading gap before the first segment and, when `duration`
    is given, the trailing gap after the last one.
    """
    if not segments:
        return []
    padded = pad_segments(segments, padding, duration)
    gaps = []
    if padded[0][0] > 0:
        gaps.append((0.0, padded[0][0]))
    for (_, prev_end), (next_start, _) in zip(padded, padded[1:]):
        if next_start > prev_end:
            gaps.append((prev_end, next_start))
    if duration is not None and duration > padded[-1][1]:
        gaps.append((padded[-1][1], duration))
    return gaps
