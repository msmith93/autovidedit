"""FastAPI server for the browser review UI.

The server owns all removal math: the UI sends decisions and gets back the
recomputed removal spans. The plan file on disk is the source of truth; if
another process (e.g. `autovidedit plan apply` run by an agent) changes it,
the server reloads it on the next request.
"""

import asyncio
import json
import re
import tempfile
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from ..core import ffprobe
from ..core.edit_plan import KEEP, REMOVE, EditPlan, removal_stats
from ..core.render import RenderCancelled, render_video

STATIC_DIR = Path(__file__).parent / "static"
WAVEFORM_POINTS = 2000
STREAM_CHUNK = 512 * 1024


def create_app(
    video_path: Path,
    preview_path: Path,
    plan_path: Path,
    default_output: Path,
) -> FastAPI:
    app = FastAPI(title="autovidedit review")

    video_duration = ffprobe.get_duration(video_path)
    plan_lock = threading.Lock()
    store = {"plan": None, "mtime": None}

    def current_plan() -> EditPlan:
        """Return the plan, reloading it if the file changed on disk."""
        mtime = plan_path.stat().st_mtime_ns
        if store["plan"] is None or store["mtime"] != mtime:
            plan = EditPlan.load(plan_path)
            if not plan.duration:
                plan.duration = round(video_duration, 3)
            if not plan.video:
                plan.video = video_path.name
            plan.ensure_gaps(plan.duration)
            store["plan"], store["mtime"] = plan, mtime
        return store["plan"]

    def save(plan: EditPlan):
        plan.save(plan_path)
        store["mtime"] = plan_path.stat().st_mtime_ns

    def removal_payload(plan: EditPlan) -> dict:
        spans = plan.segments_to_remove(video_duration)
        return {"removals": spans, "stats": removal_stats(spans)}

    render_state = {
        "running": False, "pct": 0, "msg": "", "done": False,
        "success": False, "error": None, "output": None, "cancelled": False,
    }
    render_lock = threading.Lock()
    current_cancel_event = {"event": None}

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/state")
    def state():
        with plan_lock:
            plan = current_plan()
            return {
                "video_name": video_path.name,
                "duration": video_duration,
                "default_output": str(default_output),
                "options": plan.options.model_dump(),
                "entries": [
                    e.model_dump(exclude={"words"}, exclude_none=True)
                    for e in plan.entries if e.kind != "silence" or not plan.sentences
                ],
                **removal_payload(plan),
            }

    @app.put("/api/plan")
    async def save_plan(request: Request):
        """Body: {"decisions": {id: "keep"|"remove"}, "max_pause": float?}"""
        body = await request.json()
        decisions = body.get("decisions", {})
        with plan_lock:
            plan = current_plan()
            bad = [v for v in decisions.values() if v not in (KEEP, REMOVE)]
            if bad:
                raise HTTPException(400, f"Invalid decision(s): {bad}")
            unknown = [i for i in decisions if plan.get(i) is None]
            if unknown:
                raise HTTPException(400, f"Unknown entry ids: {unknown}")
            for entry_id, decision in decisions.items():
                plan.set_decision(entry_id, decision, source="human")
            if "max_pause" in body:
                value = float(body["max_pause"])
                if not 0 <= value <= 10:
                    raise HTTPException(400, "max_pause must be between 0 and 10 seconds")
                plan.options.max_pause = value
            save(plan)
            return {"saved": len(decisions), **removal_payload(plan)}

    @app.get("/video")
    def video(request: Request):
        file_size = preview_path.stat().st_size
        range_header = request.headers.get("range")
        if not range_header:
            return FileResponse(preview_path, media_type="video/mp4")

        match = re.match(r"bytes=(\d*)-(\d*)", range_header)
        if not match:
            raise HTTPException(416, "Malformed Range header")
        start = int(match.group(1) or 0)
        end = min(int(match.group(2) or file_size - 1), file_size - 1)
        if start > end:
            raise HTTPException(416, "Invalid range")

        def stream():
            with open(preview_path, "rb") as f:
                f.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    data = f.read(min(STREAM_CHUNK, remaining))
                    if not data:
                        break
                    remaining -= len(data)
                    yield data

        headers = {
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1),
        }
        return StreamingResponse(
            stream(), status_code=206, headers=headers, media_type="video/mp4"
        )

    @app.get("/api/waveform")
    def waveform():
        cache = preview_path.with_suffix(".waveform.json")
        # Recompute if the preview was regenerated after the cache was written.
        if cache.exists() and cache.stat().st_mtime >= preview_path.stat().st_mtime:
            return JSONResponse(json.loads(cache.read_text()))

        peaks = _compute_peaks(preview_path, WAVEFORM_POINTS)
        payload = {"points": len(peaks), "peaks": peaks}
        cache.write_text(json.dumps(payload))
        return JSONResponse(payload)

    @app.post("/api/render")
    async def start_render(request: Request):
        body = await request.json()
        cancel_event = threading.Event()
        with render_lock:
            if render_state["running"]:
                raise HTTPException(409, "A render is already running")
            render_state.update(
                running=True, pct=0, msg="Starting...", done=False,
                success=False, error=None, output=None, cancelled=False,
            )
            current_cancel_event["event"] = cancel_event

        output_path = Path(body.get("output") or default_output)
        with plan_lock:
            segments = current_plan().segments_to_remove(video_duration)
        problem = None
        if not segments:
            problem = (400, "No segments are marked for removal")
        elif output_path.resolve() == video_path.resolve():
            problem = (400, "Output file cannot be the same as the input file")
        if problem:
            with render_lock:
                render_state.update(running=False, done=True)
                current_cancel_event["event"] = None
            raise HTTPException(*problem)

        def progress(pct, msg):
            with render_lock:
                render_state.update(pct=pct, msg=msg)

        def run():
            try:
                render_video(
                    video_path, output_path, segments,
                    progress=progress, cancel_event=cancel_event,
                )
                with render_lock:
                    render_state.update(
                        running=False, done=True, success=True,
                        pct=100, output=str(output_path),
                    )
            except RenderCancelled:
                with render_lock:
                    render_state.update(
                        running=False, done=True, success=False,
                        error=None, cancelled=True,
                    )
            except Exception as e:
                with render_lock:
                    render_state.update(
                        running=False, done=True, success=False, error=str(e)
                    )
            finally:
                with render_lock:
                    current_cancel_event["event"] = None

        threading.Thread(target=run, daemon=True).start()
        return removal_stats(segments)

    @app.get("/api/render/progress")
    async def render_progress():
        async def events():
            while True:
                with render_lock:
                    snapshot = dict(render_state)
                yield f"data: {json.dumps(snapshot)}\n\n"
                if snapshot["done"]:
                    return
                await asyncio.sleep(0.4)

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.post("/api/render/cancel")
    def cancel_render():
        with render_lock:
            event = current_cancel_event["event"]
            if not render_state["running"] or event is None:
                raise HTTPException(409, "No render is running")
            event.set()
            render_state.update(msg="Cancelling...")
        return {"cancelling": True}

    return app


def _compute_peaks(media_path: Path, points: int) -> list:
    """Downsample the audio to `points` normalized peak values for the waveform."""
    from pydub import AudioSegment

    with tempfile.NamedTemporaryFile(suffix=".wav") as tmp:
        ffprobe.extract_audio_wav(media_path, Path(tmp.name), sample_rate=8000)
        audio = AudioSegment.from_wav(tmp.name)

    samples = audio.get_array_of_samples()
    total = len(samples)
    if total == 0:
        return [0.0] * points
    bucket = max(1, total // points)
    full_scale = float(1 << (8 * audio.sample_width - 1))

    peaks = []
    for i in range(points):
        chunk = samples[i * bucket : (i + 1) * bucket]
        if not chunk:
            peaks.append(0.0)
            continue
        peak = max(abs(min(chunk)), abs(max(chunk))) / full_scale
        peaks.append(round(peak, 4))
    return peaks
