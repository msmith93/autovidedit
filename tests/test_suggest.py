"""AI pass tests with a fake Anthropic client (no network, no API key)."""

import json
import shutil
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from autovidedit.core.edit_plan import EditPlan
from autovidedit.core.project import Project
from autovidedit.core.suggest import (
    FALLBACK_BETA,
    SuggestOptions,
    estimate_cost,
    suggest,
    windows,
)
from tests.conftest import FIXTURE_DURATION


class FakeClient:
    """Mimics client.beta.messages.stream(...).get_final_message()."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    @contextmanager
    def _stream(self, **kwargs):
        self.requests.append(kwargs)
        reply = self.replies.pop(0)
        yield SimpleNamespace(get_final_message=lambda: reply)


def message(payload=None, stop_reason="end_turn", model="claude-opus-5"):
    return SimpleNamespace(
        stop_reason=stop_reason,
        stop_details=SimpleNamespace(category="cyber") if stop_reason == "refusal" else None,
        model=model,
        content=[] if payload is None else [SimpleNamespace(type="text", text=json.dumps(payload))],
        usage=SimpleNamespace(input_tokens=1000, output_tokens=200,
                              cache_creation_input_tokens=0, cache_read_input_tokens=500),
    )


@pytest.fixture
def project(tmp_path, fixture_video):
    video = tmp_path / "clip.mkv"
    shutil.copy(fixture_video, video)
    return Project.from_path(video)


@pytest.fixture
def plan():
    plan = EditPlan(video="clip.mkv", duration=FIXTURE_DURATION)
    plan.add_sentence(0.5, 2.9, "is it recording", 1,
                      words=[{"start": 0.5, "end": 0.8, "word": "is"}])
    plan.add_sentence(6.1, 9.0, "um so today we", 1,
                      words=[{"start": 6.1, "end": 6.4, "word": "um"},
                             {"start": 6.5, "end": 9.0, "word": "so today we"}])
    plan.add_sentence(12.0, 16.0, "second window line", 2)
    plan.ensure_gaps()
    return plan


def test_windows_join_short_tail():
    assert windows(25 * 60, 600) == [(0, 600), (600, 1200), (1200, 1500)]
    assert windows(21 * 60, 600) == [(0, 600), (600, 1260)]
    assert windows(0, 600) == []


def test_suggest_builds_requests_and_validates_answers(project, plan):
    first, second, third = plan.sentences
    client = FakeClient([
        message({
            "decisions": [
                {"id": first.id, "decision": "remove", "rationale": "setup chatter", "confidence": 0.9},
                {"id": third.id, "decision": "remove", "rationale": "wrong window", "confidence": 0.5},
                {"id": "madeup", "decision": "remove", "rationale": "?", "confidence": 0.5},
            ],
            "extra_cuts": [
                {"start": 6.1, "end": 6.4, "rationale": "filler 'um'", "confidence": 0.8},
                {"start": 30.0, "end": 31.0, "rationale": "outside", "confidence": 0.5},
            ],
        }),
        message({"decisions": [], "extra_cuts": []}, model="claude-opus-4-8"),
    ])
    options = SuggestOptions(window_minutes=10 / 60, context_minutes=0.05, frame_every=5)
    decisions, report = suggest(project, plan, options, client=client, log=lambda *_: None)

    assert [d.id for d in decisions.decisions] == [first.id]
    assert [(c.start, c.end) for c in decisions.extra_cuts] == [(6.1, 6.4)]
    assert "madeup" in report and "fallback model claude-opus-4-8" in report

    request = client.requests[0]
    assert request["model"] == "claude-opus-5"
    assert request["betas"] == [FALLBACK_BETA] and request["fallbacks"] == "default"
    assert request["output_config"]["format"]["type"] == "json_schema"
    assert request["system"][0]["cache_control"] == {"type": "ephemeral"}
    blocks = request["messages"][0]["content"]
    assert sum(b["type"] == "image" for b in blocks) == 2          # frames at 0s and 5s
    transcript = blocks[-1]["text"]
    assert f"[{first.id}]" in transcript and "um@6.10-6.40" in transcript
    assert f"CONTEXT [{third.id}]" in transcript                   # read-only neighbour

    # The merged result never overrides a human decision.
    plan.set_decision(first.id, "keep", source="human")
    result = plan.apply_decisions(decisions)
    assert result.skipped_human == [first.id] and result.cuts_added == 1


def test_refused_window_is_skipped_not_fatal(project, plan):
    client = FakeClient([message(stop_reason="refusal"),
                         message({"decisions": [], "extra_cuts": []})])
    options = SuggestOptions(window_minutes=10 / 60, frame_every=10)
    decisions, report = suggest(project, plan, options, client=client, log=lambda *_: None)
    assert decisions.decisions == [] and "declined" in report


def test_estimate_cost():
    usage = {"input_tokens": 1_000_000, "output_tokens": 100_000,
             "cache_read_input_tokens": 1_000_000}
    assert estimate_cost("claude-opus-5", usage) == pytest.approx(5 + 2.5 + 0.5)
    assert estimate_cost("some-other-model", usage) is None
