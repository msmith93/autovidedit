# autovidedit

Automatically detects and removes dead air (silence) from video files, with a
browser-based review UI to approve edits before rendering.

## How it works

1. **Preprocess** — transcribes every audio track (faster-whisper), detects
   silence common to all tracks, and writes an edit plan (JSON) plus a
   browser-friendly preview MP4.
2. **Review** — a local web UI shows the video next to a list of sentences and
   gaps. Uncheck what you don't want, keep gaps you like, then render.
3. **Render** — removed segments are cut out with ffmpeg (NVENC GPU encoding
   when available) and the result is written as a Kdenlive-friendly MOV
   (constant frame rate, CBR AAC), preserving all audio tracks.

## Requirements

- Python 3.10+
- FFmpeg on PATH (`sudo apt install ffmpeg` / `brew install ffmpeg`)
- Optional: NVIDIA GPU (NVENC + CUDA are used automatically when present)

## Installation

```bash
python -m venv venv && source venv/bin/activate
pip install -e .
```

## Usage

```bash
# One-shot: analyze (if needed) and open the review UI
autovidedit recording.mkv

# Or step by step:
autovidedit preprocess recording.mkv [--optimize-keyframes]
autovidedit review recording.mkv
autovidedit render recording.mkv          # headless render, no UI
```

Outputs land in `<video>_preprocessed/` next to the input file:

| File | Purpose |
|---|---|
| `*_modifications.json` | edit plan; review decisions are saved here |
| `*_preview.mp4` | proxy the review UI plays |
| `*_keyframe_optimized.mov` | optional GOP=5 render source for precise stream-copy cuts |
| `*_processed.mov` | final render |

### preprocess options

- `--silence-threshold` dB level counted as silence (default −40)
- `--min-silence-duration` shortest silence to remove, seconds (default 0.5)
- `--padding` buffer kept around speech, seconds (default 0.1)
- `--no-sentences` skip transcription (silence-only edit plan)
- `--whisper-model` faster-whisper size: tiny/base/small/medium/large-v3 (default medium)
- `--optimize-keyframes` re-encode with GOP=5 so stream-copy cuts are frame-precise
- `--force` re-analyze, discarding existing review decisions

### Review UI

- Checkbox = keep in video. Sentences default to kept, gaps and silences to removed.
- Click a row to jump there; shift/ctrl-click to multi-select, then **K**eep / **R**emove.
- Yellow-flagged rows overlap a removed sentence on the other audio track.
- The waveform shades everything currently marked for removal; click it to seek.
- **Render…** offers stream copy (fast; combine with `--optimize-keyframes` for
  clean cuts) or full re-encode (frame-precise, slower).

## Development

```bash
pip install -e .[dev]
pytest
```

Tests generate a small synthetic video with ffmpeg, so no fixtures are checked in.

## Future features

- Filler word removal
- Automatic zoom detection for coding walkthroughs
- Kdenlive project / EDL export of the cut list
