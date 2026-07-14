"""FastAPI server for the browser review UI."""

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
from ..core.edit_plan import KEPT, REMOVED, EditPlan
from ..core.render import render_edit_plan

STATIC_DIR = Path(__file__).parent / "static"
WAVEFORM_POINTS = 2000
STREAM_CHUNK = 512 * 1024


def create_app(
    video_name: str,
    preview_path: Path,
    render_source: Path,
    plan_path: Path,
    default_output: Path,
) -> FastAPI:
    app = FastAPI(title="autovidedit review")

    plan = EditPlan.load(plan_path)
    duration = ffprobe.get_duration(preview_path)
    plan.ensure_gaps(duration)

    render_state = {
        "running": False, "pct": 0, "msg": "", "done": False,
        "success": False, "error": None, "output": None,
    }
    render_lock = threading.Lock()

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/state")
    def state():
        return {
            "video_name": video_name,
            "render_source": render_source.name,
            "duration": duration,
            "default_output": str(default_output),
            "entries": plan.entries,
        }

    @app.put("/api/plan")
    async def save_plan(request: Request):
        body = await request.json()
        modifications = body.get("modifications", {})
        unknown = []
        for entry_id, modification in modifications.items():
            if modification not in (REMOVED, KEPT):
                raise HTTPException(400, f"Invalid modification: {modification}")
            if not plan.set_modification(entry_id, modification):
                unknown.append(entry_id)
        if unknown:
            raise HTTPException(400, f"Unknown entry ids: {unknown}")
        plan.save(plan_path)
        return {"saved": len(modifications)}

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
        if cache.exists():
            return JSONResponse(json.loads(cache.read_text()))

        peaks = _compute_peaks(preview_path, WAVEFORM_POINTS)
        payload = {"points": len(peaks), "peaks": peaks}
        cache.write_text(json.dumps(payload))
        return JSONResponse(payload)

    @app.post("/api/render")
    async def start_render(request: Request):
        body = await request.json()
        with render_lock:
            if render_state["running"]:
                raise HTTPException(409, "A render is already running")
            render_state.update(
                running=True, pct=0, msg="Starting...", done=False,
                success=False, error=None, output=None,
            )

        output_path = Path(body.get("output") or default_output)
        re_encode = bool(body.get("re_encode", False))
        segments = plan.segments_to_remove(duration)
        if not segments:
            with render_lock:
                render_state.update(running=False, done=True)
            raise HTTPException(400, "No segments are marked for removal")

        def progress(pct, msg):
            with render_lock:
                render_state.update(pct=pct, msg=msg)

        def run():
            try:
                render_edit_plan(
                    render_source, output_path, segments,
                    progress=progress, re_encode_video=re_encode,
                )
                with render_lock:
                    render_state.update(
                        running=False, done=True, success=True,
                        pct=100, output=str(output_path),
                    )
            except Exception as e:
                with render_lock:
                    render_state.update(
                        running=False, done=True, success=False, error=str(e)
                    )

        threading.Thread(target=run, daemon=True).start()
        removed_total = sum(end - start for start, end in segments)
        return {"segments": len(segments), "removed_seconds": round(removed_total, 1)}

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
