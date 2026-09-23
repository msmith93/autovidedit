# autovidedit

Cuts dead air, retakes and filler from recordings. Pipeline: faster-whisper +
pydub analysis writes a typed JSON edit plan; an optional Claude pass proposes
cuts; a FastAPI web UI reviews it; one ffmpeg pass renders MP4.

## Workflow

```bash
autovidedit <video.mkv>              # preprocess (if needed) + open review UI
autovidedit preprocess <video.mkv>   # analyze only
autovidedit suggest <video.mkv>      # Claude proposes cuts (needs [ai] + API key)
autovidedit review <video.mkv>       # review UI only (http://127.0.0.1:8791)
autovidedit render <video.mkv>       # headless render -> <stem>_edited.mp4
autovidedit transcript|frames|plan   # agent primitives, see docs/AGENT_WORKFLOW.md
```

Outputs go to `<video>_preprocessed/` next to the input. The user's
recordings live in `~/Videos/autovidedit/`, not in the repo.

## Layout

- `autovidedit/core/edit_plan.py`: the plan schema (pydantic, version 2) and
  **all removal semantics** (`segments_to_remove`). Start here.
- `autovidedit/core/pipeline.py`: `analyze` / `preprocess` / `load_plan` /
  `render` library entry points. `cli.py` and the server only wrap these.
- `autovidedit/core/project.py`: every derived path for a video.
- `autovidedit/core/render.py`: single-pass render (select+setpts video,
  Python-cut PCM audio, frame-snapped boundaries).
- `autovidedit/core/suggest.py`: Claude editing pass (prompt, schema, windows).
- `autovidedit/core/{transcribe,silence,segments,transcript,frames,mixing,ffprobe,cuda_libs}.py`
- `autovidedit/server/`: FastAPI app + static UI (vanilla JS, no build step).
  The UI does no removal math; the server returns `removals`.
- `tests/`: pytest; fixture videos are generated with ffmpeg lavfi.

## Verify

```bash
source venv/bin/activate
pip install -e .[dev,ai,gpu]
pytest        # ~15s, includes real ffmpeg renders and a lip-sync test
```

For a real-footage check, copy a recording from `~/Videos/autovidedit/` into a
scratch dir and run `preprocess`, `transcript`, `plan show`, `render` there
(don't `--force` on the originals; that discards the user's review decisions).

## Invariants

- Render output: MP4, H.264, constant frame rate at the source rate, 1 s GOP,
  every audio track preserved as AAC 192k. Kdenlive imports it; don't change
  this without checking with the user.
- Cut boundaries are snapped to the frame grid and audio is cut at the same
  snapped times. `tests/test_render.py` sync test guards A/V sync.
- The render's filtergraph must stay constant-size. A per-segment
  trim/concat graph ran at ~1x real time with 500+ cuts.
- Human decisions (`source="human"`) are never overwritten by automated
  passes unless `force=True`.
- Kept speech on any track always wins over a removed sentence on another.
- GPU transcription needs `core/cuda_libs.preload_cuda_libs()` (torch is not
  installed; it used to preload libcublas/libcudnn by accident).
- FFmpeg must be installed system-wide (`sudo apt install ffmpeg`).
