"""Endpoint tests for the FastAPI review server.

Uses fastapi.testclient.TestClient against synthetic fixture videos. The render
tests exercise the real ffmpeg pipeline, so they are the slow ones here.
"""

import glob
import json
import os
import tempfile
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from autovidedit.core import ffprobe
from autovidedit.core.edit_plan import KEEP, REMOVE, DecisionItem, DecisionSet, EditPlan
from autovidedit.core.ffprobe import get_duration, probe
from autovidedit.server import app as app_module
from autovidedit.server.app import create_app
from tests.conftest import FIXTURE_DURATION, SILENCE_SPANS


def _removal_plan(path: Path) -> EditPlan:
    """Two kept sentences with removable pauses around them."""
    plan = EditPlan(duration=FIXTURE_DURATION)
    plan.add_sentence(1.0, 3.0, "first track speaking here", 1)
    plan.add_sentence(7.0, 12.0, "second track speaking now", 2)
    for start, end in SILENCE_SPANS:
        plan.add_silence(start, end)
    plan.ensure_gaps()
    plan.save(path)
    return plan


def _no_removal_plan(path: Path) -> EditPlan:
    """Nothing marked for removal: one kept sentence and a kept gap."""
    plan = EditPlan(duration=FIXTURE_DURATION)
    plan.add_sentence(1.0, 5.0, "only sentence", 1)
    gap = plan.add_gap(5.0, 8.0)
    plan.set_decision(gap.id, KEEP)
    plan.save(path)
    return plan


def _make_client(tmp_path, fixture_video, fixture_video_2track,
                 build_plan=_removal_plan, output_name="out.mp4") -> TestClient:
    plan_path = tmp_path / "plan.json"
    build_plan(plan_path)
    app = create_app(
        video_path=fixture_video_2track,
        preview_path=fixture_video,
        plan_path=plan_path,
        default_output=tmp_path / output_name,
    )
    return TestClient(app)


@pytest.fixture
def client(tmp_path, fixture_video, fixture_video_2track):
    return _make_client(tmp_path, fixture_video, fixture_video_2track)


def _drain_progress(client: TestClient, timeout: float = 90.0) -> dict:
    """Read the SSE progress stream until the render reports done."""
    deadline = time.time() + timeout
    with client.stream("GET", "/api/render/progress") as resp:
        assert resp.status_code == 200
        for line in resp.iter_lines():
            if line.startswith("data: "):
                snapshot = json.loads(line[len("data: "):])
                if snapshot["done"]:
                    return snapshot
            if time.time() > deadline:
                raise TimeoutError("render did not finish in time")
    raise RuntimeError("progress stream ended before done")


# ---------- static + state ----------

def test_index_html(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_state(client):
    data = client.get("/api/state").json()
    assert abs(data["duration"] - FIXTURE_DURATION) < 0.5
    assert data["entries"] and all(e.get("id") for e in data["entries"])
    # Silences are an analyzer detail when sentences exist; the UI doesn't list them.
    assert {e["kind"] for e in data["entries"]} == {"sentence", "gap"}
    assert data["removals"] and data["stats"]["count"] == len(data["removals"])
    assert data["options"]["max_pause"] == 0.5


# ---------- plan editing ----------

def test_plan_put_persists_as_human_and_returns_removals(client, tmp_path):
    sentence = next(e for e in client.get("/api/state").json()["entries"]
                    if e["kind"] == "sentence")
    resp = client.put("/api/plan", json={"decisions": {sentence["id"]: REMOVE}})
    assert resp.status_code == 200
    removals = resp.json()["removals"]
    assert any(s <= sentence["start"] and sentence["end"] <= e for s, e in removals)

    saved = EditPlan.load(tmp_path / "plan.json").get(sentence["id"])
    assert saved.decision == REMOVE and saved.source == "human"


def test_plan_put_max_pause(client, tmp_path):
    before = client.get("/api/state").json()["stats"]["seconds"]
    resp = client.put("/api/plan", json={"max_pause": 0.0})
    assert resp.status_code == 200
    assert resp.json()["stats"]["seconds"] > before
    assert EditPlan.load(tmp_path / "plan.json").options.max_pause == 0.0


def test_plan_put_unknown_id(client):
    resp = client.put("/api/plan", json={"decisions": {"deadbeef0000": REMOVE}})
    assert resp.status_code == 400


def test_plan_put_invalid_value(client):
    entry_id = client.get("/api/state").json()["entries"][0]["id"]
    resp = client.put("/api/plan", json={"decisions": {entry_id: "MAYBE"}})
    assert resp.status_code == 400


def test_server_picks_up_plan_edits_made_on_disk(client, tmp_path):
    """An agent running `plan apply` while the UI is open must not be clobbered."""
    plan_path = tmp_path / "plan.json"
    sentence = EditPlan.load(plan_path).sentences[0]
    client.get("/api/state")

    plan = EditPlan.load(plan_path)
    plan.apply_decisions(DecisionSet(decisions=[
        DecisionItem(id=sentence.id, decision=REMOVE, rationale="agent says cut"),
    ]))
    plan.save(plan_path)
    os.utime(plan_path, ns=(time.time_ns(), time.time_ns() + 10_000_000))

    entry = next(e for e in client.get("/api/state").json()["entries"]
                 if e["id"] == sentence.id)
    assert entry["decision"] == REMOVE and entry["rationale"] == "agent says cut"


# ---------- video streaming ----------

def test_video_full(client):
    resp = client.get("/video")
    assert resp.status_code == 200
    assert len(resp.content) > 0


def test_video_range(client):
    resp = client.get("/video", headers={"Range": "bytes=0-99"})
    assert resp.status_code == 206
    assert resp.headers["Content-Length"] == "100"
    assert resp.headers["Content-Range"].startswith("bytes 0-99/")
    assert len(resp.content) == 100


def test_video_range_unsatisfiable(client):
    resp = client.get("/video", headers={"Range": "bytes=5-2"})
    assert resp.status_code == 416


# ---------- waveform ----------

def test_waveform_shape_and_cache(client, fixture_video, monkeypatch):
    cache = fixture_video.with_suffix(".waveform.json")
    cache.unlink(missing_ok=True)

    resp = client.get("/api/waveform")
    assert resp.status_code == 200
    peaks = resp.json()["peaks"]
    assert len(peaks) == 2000
    assert all(0.0 <= p <= 1.0 for p in peaks)
    assert cache.exists()

    def _boom(*a, **k):
        raise AssertionError("waveform should have been served from cache")

    monkeypatch.setattr(app_module, "_compute_peaks", _boom)
    resp2 = client.get("/api/waveform")
    assert resp2.status_code == 200
    assert resp2.json()["peaks"] == peaks


def test_waveform_cache_refreshes_when_preview_is_newer(client, fixture_video, monkeypatch):
    cache = fixture_video.with_suffix(".waveform.json")
    cache.write_text(json.dumps({"points": 1, "peaks": [0.5]}))
    old = fixture_video.stat().st_mtime - 100
    os.utime(cache, (old, old))
    monkeypatch.setattr(app_module, "_compute_peaks", lambda path, points: [0.1] * points)
    assert client.get("/api/waveform").json()["peaks"][:2] == [0.1, 0.1]
    cache.unlink()


# ---------- render ----------

def test_render_no_segments_400(tmp_path, fixture_video, fixture_video_2track):
    client = _make_client(
        tmp_path, fixture_video, fixture_video_2track, build_plan=_no_removal_plan
    )
    resp = client.post("/api/render", json={})
    assert resp.status_code == 400
    # A rejected render must not leave the server stuck in "running".
    assert client.post("/api/render/cancel").status_code == 409


def test_render_produces_two_track_mp4(client, tmp_path):
    output = tmp_path / "out.mp4"
    resp = client.post("/api/render", json={"output": str(output)})
    assert resp.status_code == 200
    assert resp.json()["count"] > 0

    snapshot = _drain_progress(client)
    assert snapshot["success"] is True, snapshot
    assert output.exists()

    assert get_duration(output) < FIXTURE_DURATION - 1.0
    audio = [s for s in probe(output)["streams"] if s["codec_type"] == "audio"]
    assert len(audio) == 2
    assert all(s["codec_name"] == "aac" for s in audio)
    # The loud (pink noise) track lands near the 192k target; quiet tracks
    # undershoot because AAC is ABR, which is expected.
    assert abs(int(audio[0]["bit_rate"]) - 192_000) <= 0.15 * 192_000


# ---------- cancellation ----------

def test_cancel_when_idle_409(client):
    assert client.post("/api/render/cancel").status_code == 409


def test_cancel_mid_render(tmp_path, fixture_video, fixture_video_2track, monkeypatch):
    # Force libx264 so the encode is slow enough to cancel.
    monkeypatch.setattr(ffprobe, "has_nvenc", lambda: False)
    before = set(glob.glob(str(Path(tempfile.gettempdir()) / "autovidedit_render_*")))

    client = _make_client(tmp_path, fixture_video, fixture_video_2track)
    output = tmp_path / "cancelled.mp4"
    resp = client.post("/api/render", json={"output": str(output)})
    assert resp.status_code == 200

    cancel = client.post("/api/render/cancel")
    assert cancel.status_code == 200
    assert cancel.json()["cancelling"] is True

    snapshot = _drain_progress(client)
    assert snapshot["cancelled"] is True
    assert snapshot["success"] is False
    assert not output.exists()

    leftover = set(glob.glob(str(Path(tempfile.gettempdir()) / "autovidedit_render_*")))
    assert leftover == before, f"leftover temp dirs: {leftover - before}"
