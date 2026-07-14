import json

from autovidedit.core.edit_plan import KEPT, REMOVED, EditPlan


def make_plan() -> EditPlan:
    plan = EditPlan()
    plan.add_sentence(1.0, 3.0, "first sentence", audio_track=1)
    plan.add_sentence(5.0, 7.0, "second sentence", audio_track=2)
    plan.add_silence(1.8, 2.4)   # inside first sentence
    plan.add_silence(8.0, 9.0)   # outside any sentence
    return plan


def test_round_trip_preserves_ids(tmp_path):
    plan = make_plan()
    path = tmp_path / "plan.json"
    plan.save(path)

    loaded = EditPlan.load(path)
    assert {e["id"] for e in loaded.entries} == {e["id"] for e in plan.entries}
    assert len(loaded.sentences) == 2
    assert len(loaded.silences) == 2


def test_load_assigns_missing_ids(tmp_path):
    path = tmp_path / "legacy.json"
    legacy = [
        {"start_time": 0.5, "end_time": 1.0, "modification": "REMOVED", "reason": "dead air"},
    ]
    path.write_text(json.dumps(legacy))
    plan = EditPlan.load(path)
    assert all(e.get("id") for e in plan.entries)


def test_ensure_gaps_derives_from_sentences():
    plan = make_plan()
    plan.ensure_gaps(video_duration=10.0)
    gap_ranges = [(g["start_time"], g["end_time"]) for g in plan.gaps]
    # Sentences padded by 0.2: [0.8-3.2], [4.8-7.2]
    assert gap_ranges == [(0.0, 0.8), (3.2, 4.8), (7.2, 10.0)]
    assert all(g["modification"] == REMOVED for g in plan.gaps)

    # Calling again must not duplicate
    plan.ensure_gaps(video_duration=10.0)
    assert len(plan.gaps) == 3


def test_segments_to_remove_with_sentences():
    plan = make_plan()
    plan.ensure_gaps(video_duration=10.0)

    # Remove the second sentence, keep the middle gap
    second = plan.sentences[1]
    plan.set_modification(second["id"], REMOVED)
    middle_gap = plan.gaps[1]
    plan.set_modification(middle_gap["id"], KEPT)

    removal = plan.segments_to_remove(video_duration=10.0)
    # Expected: leading gap 0-0.8, silence-in-kept-sentence 1.8-2.4,
    # removed sentence 5-7 merged with trailing gap 7.2-10 -> not adjacent, so separate
    assert removal == [(0.0, 0.8), (1.8, 2.4), (5.0, 7.0), (7.2, 10.0)]


def test_segments_to_remove_silence_only_plan():
    plan = EditPlan()
    plan.add_silence(2.0, 3.0)
    plan.add_silence(5.0, 6.0)
    plan.entries[1]["modification"] = KEPT  # reviewer kept this one
    assert plan.segments_to_remove(video_duration=10.0) == [(2.0, 3.0)]


def test_set_modification_by_id():
    plan = make_plan()
    target = plan.sentences[0]
    assert plan.set_modification(target["id"], REMOVED)
    assert plan.get(target["id"])["modification"] == REMOVED
    assert not plan.set_modification("nonexistent", REMOVED)
