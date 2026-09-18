"""End-to-end CLI tests: the workflow an agent drives, on the synthetic video."""

import json
import shutil
from pathlib import Path

import pytest

from autovidedit.cli import main
from autovidedit.core import ffprobe
from autovidedit.core.edit_plan import EditPlan
from autovidedit.core.project import Project
from tests.conftest import FIXTURE_DURATION


@pytest.fixture
def project(tmp_path, fixture_video):
    video = tmp_path / "clip.mkv"
    shutil.copy(fixture_video, video)
    return Project.from_path(video)


def run(capsys, *argv) -> str:
    main([str(a) for a in argv])
    return capsys.readouterr().out


def test_preprocess_silence_only_then_render(project, capsys):
    run(capsys, "preprocess", project.video, "--no-sentences")
    assert project.plan_path.exists() and project.preview_path.exists()

    plan = EditPlan.load(project.plan_path)
    assert plan.version == 2 and plan.video == "clip.mkv"
    assert len(plan.silences) == 2

    with pytest.raises(SystemExit, match="already exists"):
        run(capsys, "preprocess", project.video, "--no-sentences")

    out = run(capsys, "render", project.video)
    assert project.output_path.exists(), out
    # Two silences (2.8s and 0.8s after detection padding), each kept to 0.5s.
    assert abs(ffprobe.get_duration(project.output_path) - (FIXTURE_DURATION - 2.6)) < 0.3


def _sentence_plan(project) -> EditPlan:
    project.ensure_out_dir()
    plan = EditPlan(video=project.video.name, duration=FIXTURE_DURATION)
    plan.add_sentence(0.5, 2.9, "Okay so the first take", 1)
    plan.add_sentence(6.1, 11.9, "Okay so the second, better take", 1)
    plan.add_sentence(13.1, 19.5, "and that is the point", 2)
    plan.add_silence(3.1, 5.9)
    plan.add_silence(12.1, 12.9)
    plan.ensure_gaps()
    plan.save(project.plan_path)
    return plan


def test_transcript_text_and_json(project, capsys):
    plan = _sentence_plan(project)
    text = run(capsys, "transcript", project.video)
    first = plan.sentences[0]
    assert f"[{first.id}] 0:00.5-0:02.9  KEEP    T1  Okay so the first take" in text
    assert "(gap " in text and "silence" not in text

    data = json.loads(run(capsys, "transcript", project.video, "--json", "--start", "12"))
    assert [e["kind"] for e in data][-1] == "sentence"
    assert all(e["end"] > 12 for e in data)


def test_plan_set_apply_show(project, capsys, tmp_path):
    plan = _sentence_plan(project)
    first, second, third = plan.sentences

    out = run(capsys, "plan", "set", project.video, first.id, "--remove",
              "--rationale", "false start")
    assert "REMOVE" in out and "human: false start" in out

    decisions = {
        "decisions": [
            {"id": first.id, "decision": "keep", "rationale": "AI disagrees"},
            {"id": third.id, "decision": "remove", "rationale": "off topic", "confidence": 0.7},
        ],
        "extra_cuts": [{"start": 7.0, "end": 7.4, "rationale": "um"}],
    }
    path = tmp_path / "decisions.json"
    path.write_text(json.dumps(decisions))
    result = json.loads(run(capsys, "plan", "apply", project.video, path))
    assert result == {"applied": 1, "unchanged": 0, "skipped_human": [first.id], "cuts_added": 1}

    saved = EditPlan.load(project.plan_path)
    assert saved.get(first.id).decision == "remove"          # human choice survived
    assert saved.get(third.id).source == "ai"
    assert saved.cuts[0].rationale == "um"

    summary = json.loads(run(capsys, "plan", "show", project.video))
    assert summary["sources"]["human"] == 1 and summary["sources"]["ai"] == 2
    assert summary["output_duration"] < FIXTURE_DURATION

    run(capsys, "plan", "options", project.video, "--max-pause", "1.0")
    assert EditPlan.load(project.plan_path).options.max_pause == 1.0


def test_plan_apply_rejects_unknown_ids(project, capsys, tmp_path):
    _sentence_plan(project)
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"decisions": [{"id": "nope", "decision": "remove"}]}))
    with pytest.raises(SystemExit, match="Unknown entry ids"):
        run(capsys, "plan", "apply", project.video, path)


def test_frames(project, capsys):
    run(capsys, "frames", project.video, "--every", "5")
    index = json.loads((project.frames_dir / "frames.json").read_text())
    assert [f["time"] for f in index] == [0.0, 5.0, 10.0, 15.0]
    assert all(Path(f["file"]).stat().st_size > 0 for f in index)

    out = run(capsys, "frames", project.video, "--at", "7.25")
    assert out.strip().endswith("t_000007250.jpg")


def test_missing_plan_message(project, capsys):
    with pytest.raises(SystemExit, match="No edit plan"):
        run(capsys, "transcript", project.video)
