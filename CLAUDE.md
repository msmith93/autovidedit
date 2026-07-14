# autovidedit

Removes dead air/silence from video files: analysis pipeline (faster-whisper +
pydub) writes a JSON edit plan, a FastAPI web UI reviews it, ffmpeg renders the
cuts. NVENC GPU acceleration is used automatically when available.

## Workflow

```bash
autovidedit <video.mkv>              # preprocess (if needed) + open review UI
autovidedit preprocess <video.mkv>   # analyze only
autovidedit review <video.mkv>       # review UI only (http://127.0.0.1:8765)
autovidedit render <video.mkv>       # headless render from saved plan
```

Outputs go to `<video>_preprocessed/` next to the input.

## Layout

- `autovidedit/core/` — pipeline: `segments.py` (pure segment math),
  `edit_plan.py` (JSON plan model + removal computation), `silence.py`,
  `transcribe.py`, `mixing.py` (preview proxy), `render.py`, `ffprobe.py`
  (all ffmpeg subprocess wrappers)
- `autovidedit/server/` — FastAPI app + static web UI (vanilla JS, no build step)
- `autovidedit/cli.py` — entry point
- `tests/` — `pytest`; fixture video is generated with ffmpeg lavfi, no media checked in

## Setup

```bash
source venv/bin/activate
pip install -e .[dev]
pytest        # fast; includes real ffmpeg render tests
```

FFmpeg must be installed system-wide (`sudo apt install ffmpeg`).
Render output stays Kdenlive-compatible: MOV, constant frame rate, CBR AAC 192k,
all audio tracks preserved — don't change this without checking with the user.
