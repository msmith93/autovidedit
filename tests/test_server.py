"""Endpoint tests for the FastAPI review server (tests/test_server.py).

Uses fastapi.testclient.TestClient against synthetic fixture videos. The render
tests exercise the real ffmpeg pipeline, so they are the slow ones here.
"""

import glob
import json
import tempfile
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from autovidedit.core import ffprobe
from autovidedit.core.edit_plan import KEPT, REMOVED, EditPlan
from autovidedit.core.ffprobe import get_duration, probe
from autovidedit.server import app as app_module
from autovidedit.server.app import create_app
from tests.conftest import FIXTURE_DURATION, SILENCE_SPANS


def _removal_plan(path: Path) -> EditPlan:
    """A plan that removes real time: two kept sentences with gaps between."""
    plan = EditPlan()
    plan.add_sentence(1.0, 3.0, "first track speaking here", 1)
    plan.add_sentence(7.0, 12.0, "second track speaking now", 2)
    for start, end in SILENCE_SPANS:
        plan.add_silence(start, end)
    plan.ensure_gaps(FIXTURE_DURATION)
    plan.save(path)
    return plan


def _no_removal_plan(path: Path) -> EditPlan:
    """A plan with nothing marked for removal (a kept sentence + kept gap)."""
    plan = EditPlan()
    plan.add_sentence(1.0, 5.0, "only sentence", 1)
    gap = plan.add_gap(5.0, 8.0)
    plan.set_modification(gap["id"], KEPT)  # keep it so ensure_gaps stays a no-op
    plan.save(path)
    return plan


def _make_client(tmp_path, fixture_video, fixture_video_2track,
                 build_plan=_removal_plan, output_name="out.mov") -> TestClient:
    plan_path = tmp_path / "plan.json"
    build_plan(plan_path)
    app = create_app(
        video_name="test_input.mkv",
        preview_path=fixture_video,
        render_source=fixture_video_2track,
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
    resp = client.get("/api/state")
    assert resp.status_code == 200
    data = resp.json()
    assert abs(data["duration"] - FIXTURE_DURATION) < 0.5
    assert data["entries"], "no entries returned"
    assert all(e.get("id") for e in data["entries"])


# ---------- plan editing ----------

def test_plan_put_persists(client, tmp_path):
    entry_id = client.get("/api/state").json()["entries"][0]["id"]
    resp = client.put("/api/plan", json={"modifications": {entry_id: REMOVED}})
    assert resp.status_code == 200

    saved = json.loads((tmp_path / "plan.json").read_text())
    entry = next(e for e in saved if e["id"] == entry_id)
    assert entry["modification"] == REMOVED


def test_plan_put_unknown_id(client):
    resp = client.put("/api/plan", json={"modifications": {"deadbeef0000": REMOVED}})
    assert resp.status_code == 400


def test_plan_put_invalid_value(client):
    entry_id = client.get("/api/state").json()["entries"][0]["id"]
    resp = client.put("/api/plan", json={"modifications": {entry_id: "MAYBE"}})
    assert resp.status_code == 400


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

    # Second call must come from cache: break the computer and expect success.
    def _boom(*a, **k):
        raise AssertionError("waveform should have been served from cache")

    monkeypatch.setattr(app_module, "_compute_peaks", _boom)
    resp2 = client.get("/api/waveform")
    assert resp2.status_code == 200
    assert resp2.json()["peaks"] == peaks


# ---------- render ----------

def test_render_no_segments_400(tmp_path, fixture_video, fixture_video_2track):
    client = _make_client(
        tmp_path, fixture_video, fixture_video_2track, build_plan=_no_removal_plan
    )
    resp = client.post("/api/render", json={})
    assert resp.status_code == 400


def test_render_produces_two_track_mov(client, tmp_path):
    output = tmp_path / "out.mov"
    resp = client.post("/api/render", json={"output": str(output), "re_encode": False})
    assert resp.status_code == 200
    assert resp.json()["segments"] > 0

    snapshot = _drain_progress(client)
    assert snapshot["success"] is True
    assert output.exists()

    assert get_duration(output) < FIXTURE_DURATION - 1.0
    audio = [s for s in probe(output)["streams"] if s["codec_type"] == "audio"]
    assert len(audio) == 2
    assert all(s["codec_name"] == "aac" for s in audio)


def test_convert_to_mov_loud_track_bitrate(tmp_path, fixture_video_2track):
    """Task B: per-stream 192k intent; the loud (incompressible) track lands
    near target, the quiet track undershoots (ABR encoder, acceptable)."""
    from autovidedit.core.render import VideoRenderer

    output = tmp_path / "bitrate.mov"
    VideoRenderer().convert_to_mov(fixture_video_2track, output)

    audio = [s for s in probe(output)["streams"] if s["codec_type"] == "audio"]
    assert len(audio) == 2
    assert all(s["codec_name"] == "aac" for s in audio)
    loud_bitrate = int(audio[0]["bit_rate"])
    assert abs(loud_bitrate - 192_000) <= 0.15 * 192_000


# ---------- cancellation ----------

def test_cancel_when_idle_409(client):
    resp = client.post("/api/render/cancel")
    assert resp.status_code == 409


def test_cancel_mid_render(tmp_path, fixture_video, fixture_video_2track, monkeypatch):
    # Force libx264 (no NVENC) so the re-encode is slow enough to cancel.
    monkeypatch.setattr(ffprobe, "has_nvenc", lambda: False)
    before = set(glob.glob(str(Path(tempfile.gettempdir()) / "autovidedit_segments_*")))

    client = _make_client(tmp_path, fixture_video, fixture_video_2track)
    output = tmp_path / "cancelled.mov"
    resp = client.post("/api/render", json={"output": str(output), "re_encode": True})
    assert resp.status_code == 200

    cancel = client.post("/api/render/cancel")
    assert cancel.status_code == 200
    assert cancel.json()["cancelling"] is True

    snapshot = _drain_progress(client)
    assert snapshot["cancelled"] is True
    assert snapshot["success"] is False

    leftover = set(glob.glob(str(Path(tempfile.gettempdir()) / "autovidedit_segments_*")))
    assert leftover == before, f"leftover temp dirs: {leftover - before}"
