import json

import pytest

from autovidedit.core.edit_plan import (
    KEEP,
    REMOVE,
    CutItem,
    DecisionItem,
    DecisionSet,
    EditPlan,
    PlanOptions,
)


def approx_spans(actual, expected, tol=1e-6):
    assert len(actual) == len(expected), f"{actual} != {expected}"
    for (a_s, a_e), (e_s, e_e) in zip(actual, expected):
        assert abs(a_s - e_s) < tol and abs(a_e - e_e) < tol, f"{actual} != {expected}"


def plan_with(max_pause=0.5, duration=20.0) -> EditPlan:
    return EditPlan(duration=duration, options=PlanOptions(max_pause=max_pause))


# ---------- persistence ----------

def test_round_trip_preserves_everything(tmp_path):
    plan = plan_with()
    plan.add_sentence(1.0, 3.0, "hello there", 1,
                      words=[{"start": 1.0, "end": 1.5, "word": " hello"},
                             {"start": 1.6, "end": 3.0, "word": " there"}])
    plan.add_silence(4.0, 5.0)
    plan.add_cut(6.0, 7.0, rationale="retake")
    path = tmp_path / "plan.json"
    plan.save(path)

    loaded = EditPlan.load(path)
    assert loaded.model_dump() == plan.model_dump()
    assert loaded.sentences[0].words[0].word == "hello"
    assert json.loads(path.read_text())["version"] == 2


def test_save_is_atomic_and_leaves_no_temp_files(tmp_path):
    plan = plan_with()
    plan.add_silence(1.0, 2.0)
    plan.save(tmp_path / "plan.json")
    assert [p.name for p in tmp_path.iterdir()] == ["plan.json"]


def test_migrates_v1_plan(tmp_path):
    legacy = [
        {"id": "a1", "start_time": 0.5, "end_time": 1.0, "modification": "REMOVED",
         "reason": "dead air", "duration": 0.5},
        {"id": "b2", "start_time": 1.0, "end_time": 3.0, "modification": "NONE",
         "reason": "Sentence", "content": {"audio_track": 2, "words": "kept by default"}},
        {"id": "c3", "start_time": 3.0, "end_time": 5.0, "modification": "REMOVED",
         "reason": "Sentence", "content": {"audio_track": 1, "words": "reviewer removed"}},
        {"start_time": 5.0, "end_time": 6.0, "modification": "NONE", "reason": "gap"},
    ]
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(legacy))
    plan = EditPlan.load(path)

    by_id = {e.id: e for e in plan.entries}
    assert by_id["a1"].kind == "silence" and by_id["a1"].source == "analyzer"
    assert by_id["b2"].text == "kept by default" and by_id["b2"].track == 2
    assert by_id["b2"].source == "analyzer"
    # Non-default decisions can only have come from a reviewer.
    assert by_id["c3"].decision == REMOVE and by_id["c3"].source == "human"
    gap = plan.gaps[0]
    assert gap.decision == KEEP and gap.source == "human" and gap.id


# ---------- gaps ----------

def test_ensure_gaps_derives_from_sentences_and_skips_short_ones():
    plan = plan_with(duration=10.0)
    plan.add_sentence(1.0, 3.0, "first", 1)
    plan.add_sentence(3.6, 5.0, "close behind", 1)   # 0.6s apart -> padded gap 0.2s, skipped
    plan.add_sentence(7.0, 8.0, "later", 2)
    plan.ensure_gaps()
    assert [(g.start, g.end) for g in plan.gaps] == [(0.0, 0.8), (5.2, 6.8), (8.2, 10.0)]
    assert all(g.decision == REMOVE for g in plan.gaps)

    plan.ensure_gaps()
    assert len(plan.gaps) == 3


# ---------- removal computation ----------

def test_long_pause_between_sentences_is_shortened_to_max_pause():
    plan = plan_with(duration=12.0)
    plan.add_sentence(0.0, 5.0, "a", 1)
    plan.add_sentence(8.0, 12.0, "b", 1)
    plan.add_silence(5.0, 8.0)
    approx_spans(plan.segments_to_remove(), [(5.25, 7.75)])


def test_pause_shorter_than_max_pause_is_not_cut():
    plan = plan_with(duration=12.0)
    plan.add_sentence(0.0, 5.0, "a", 1)
    plan.add_sentence(5.4, 12.0, "b", 1)
    plan.add_silence(5.0, 5.4)
    assert plan.segments_to_remove() == []


def test_pause_inside_kept_sentence_and_gap():
    plan = plan_with(duration=12.0)
    plan.add_sentence(0.0, 5.0, "a", 1)
    plan.add_sentence(8.0, 12.0, "b", 1)
    plan.add_gap(5.0, 8.0)
    plan.add_silence(2.0, 3.0)
    approx_spans(plan.segments_to_remove(), [(2.25, 2.75), (5.25, 7.75)])


def test_kept_gap_is_left_alone():
    plan = plan_with(duration=12.0)
    plan.add_sentence(0.0, 5.0, "a", 1)
    plan.add_sentence(8.0, 12.0, "b", 1)
    gap = plan.add_gap(5.0, 8.0)
    plan.add_silence(5.0, 8.0)
    plan.set_decision(gap.id, KEEP)
    assert plan.segments_to_remove() == []


def test_removed_sentence_never_clips_kept_speech_on_other_track():
    plan = plan_with(duration=20.0)
    removed = plan.add_sentence(10.0, 14.0, "talking over", 1)
    plan.add_sentence(12.0, 13.0, "kept reply", 2)
    plan.set_decision(removed.id, REMOVE)
    approx_spans(plan.segments_to_remove(), [(10.0, 12.0), (13.0, 14.0)])


def test_pause_next_to_removed_content_is_cut_flush():
    plan = plan_with(duration=20.0)
    removed = plan.add_sentence(10.0, 14.0, "cut me", 1)
    plan.add_sentence(16.0, 20.0, "keep me", 1)
    plan.add_gap(14.0, 16.0)
    plan.set_decision(removed.id, REMOVE)
    approx_spans(plan.segments_to_remove(), [(10.0, 15.75)])


def test_pauses_at_video_edges_are_cut_flush():
    plan = plan_with(duration=20.0)
    plan.add_sentence(3.0, 17.0, "middle", 1)
    plan.add_gap(0.0, 3.0)
    plan.add_gap(17.0, 20.0)
    approx_spans(plan.segments_to_remove(), [(0.0, 2.75), (17.25, 20.0)])


def test_cut_entries_are_removed_in_full():
    plan = plan_with(duration=20.0)
    plan.add_sentence(0.0, 10.0, "long take", 1)
    plan.add_cut(4.0, 4.4, rationale="um")
    approx_spans(plan.segments_to_remove(), [(4.0, 4.4)])


def test_silence_only_plan_shortens_removed_silences():
    plan = plan_with(duration=10.0)
    plan.add_silence(2.0, 3.0)
    kept = plan.add_silence(5.0, 6.0)
    plan.set_decision(kept.id, KEEP)
    approx_spans(plan.segments_to_remove(), [(2.25, 2.75)])


def test_max_pause_zero_restores_hard_cuts():
    plan = plan_with(max_pause=0.0, duration=12.0)
    plan.add_sentence(0.0, 5.0, "a", 1)
    plan.add_sentence(8.0, 12.0, "b", 1)
    plan.add_silence(5.0, 8.0)
    approx_spans(plan.segments_to_remove(), [(5.0, 8.0)])


# ---------- hallucination guard ----------

def test_sentence_inside_silence_is_flagged():
    plan = plan_with(duration=60.0)
    ghost = plan.add_sentence(54.7, 55.4, "You", 1)
    real = plan.add_sentence(10.0, 14.0, "an actual sentence", 1)
    plan.add_silence(54.0, 56.0)
    flagged = plan.flag_hallucinations()
    assert flagged == [ghost]
    assert ghost.decision == REMOVE and ghost.source == "analyzer" and ghost.rationale
    assert real.decision == KEEP


def test_short_stock_phrase_is_flagged_even_with_audio():
    plan = plan_with(duration=60.0)
    phrase = plan.add_sentence(20.0, 20.8, "Thank you.", 1)
    long_phrase = plan.add_sentence(30.0, 33.0, "Thank you.", 1)
    plan.flag_hallucinations()
    assert phrase.decision == REMOVE
    assert long_phrase.decision == KEEP


def test_hallucination_guard_respects_human_decisions():
    plan = plan_with(duration=60.0)
    ghost = plan.add_sentence(54.7, 55.4, "You", 1)
    plan.add_silence(54.0, 56.0)
    plan.set_decision(ghost.id, KEEP, source="human")
    assert plan.flag_hallucinations() == []
    assert ghost.decision == KEEP


# ---------- decisions ----------

def test_apply_decisions_protects_human_choices():
    plan = plan_with()
    a = plan.add_sentence(1.0, 2.0, "a", 1)
    b = plan.add_sentence(3.0, 4.0, "b", 1)
    plan.set_decision(b.id, KEEP, source="human")

    result = plan.apply_decisions(DecisionSet(
        decisions=[
            DecisionItem(id=a.id, decision=REMOVE, rationale="false start", confidence=0.9),
            DecisionItem(id=b.id, decision=REMOVE, rationale="off topic"),
        ],
        extra_cuts=[CutItem(start=5.0, end=5.3, rationale="um")],
    ))
    assert result.applied == 1 and result.skipped_human == [b.id] and result.cuts_added == 1
    assert a.decision == REMOVE and a.source == "ai" and a.rationale == "false start"
    assert b.decision == KEEP and b.source == "human"
    assert plan.cuts[0].rationale == "um" and plan.cuts[0].source == "ai"

    forced = plan.apply_decisions(
        DecisionSet(decisions=[DecisionItem(id=b.id, decision=REMOVE)]), force=True
    )
    assert forced.applied == 1 and b.decision == REMOVE


def test_apply_decisions_rejects_unknown_ids_without_changes():
    plan = plan_with()
    a = plan.add_sentence(1.0, 2.0, "a", 1)
    with pytest.raises(ValueError, match="nope"):
        plan.apply_decisions(DecisionSet(decisions=[
            DecisionItem(id=a.id, decision=REMOVE),
            DecisionItem(id="nope", decision=REMOVE),
        ]))
    assert a.decision == KEEP


def test_apply_decisions_does_not_duplicate_cuts():
    plan = plan_with()
    cuts = DecisionSet(extra_cuts=[CutItem(start=5.0, end=5.3)])
    plan.apply_decisions(cuts)
    assert plan.apply_decisions(cuts).cuts_added == 0
    assert len(plan.cuts) == 1


def test_set_decision_by_id():
    plan = plan_with()
    target = plan.add_sentence(1.0, 2.0, "a", 1)
    assert plan.set_decision(target.id, REMOVE)
    assert plan.get(target.id).decision == REMOVE
    assert plan.get(target.id).source == "human"
    assert not plan.set_decision("nonexistent", REMOVE)
