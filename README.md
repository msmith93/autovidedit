# autovidedit

Cuts dead air, retakes and filler out of recordings (screen recordings,
gameplay sessions, podcasts with one audio track per speaker), with an
optional AI editing pass and a browser review step before anything is
rendered.

## How it works

1. **Preprocess.** Transcribes every audio track with faster-whisper (GPU when
   available), detects silence common to all tracks, and writes an edit plan
   (JSON) plus a browser-friendly preview.
2. **Suggest (optional).** Claude reads the transcript and looks at frames,
   then proposes cuts for false starts, setup chatter, dead stretches and
   filler words, each with a reason.
3. **Review.** A local web UI shows the video next to the transcript. Toggle
   what to keep; changes save immediately.
4. **Render.** One ffmpeg pass writes an editor-friendly MP4 (H.264, constant
   frame rate, every audio track as AAC 192k) that imports cleanly into
   Kdenlive.

Pauses are shortened rather than deleted: anything longer than `max_pause`
(0.5 s by default) is trimmed to that length, so speech keeps its rhythm.

## Requirements

- Python 3.10+
- FFmpeg on PATH (`sudo apt install ffmpeg`)
- Optional: NVIDIA GPU. NVENC is used for encoding automatically. GPU
  transcription needs the CUDA libraries: `pip install -e .[gpu]`.

## Installation

```bash
python -m venv venv && source venv/bin/activate
pip install -e .            # add [gpu], [ai], [dev] as needed
```

## Usage

```bash
autovidedit recording.mkv                 # preprocess if needed, open the review UI

autovidedit preprocess recording.mkv      # analyze only
autovidedit suggest recording.mkv         # AI pass (needs [ai] and an API key)
autovidedit review recording.mkv          # review UI at http://127.0.0.1:8791
autovidedit render recording.mkv          # headless render
```

Commands for scripting and AI agents (see `docs/AGENT_WORKFLOW.md`):

```bash
autovidedit transcript recording.mkv [--json --words --start S --end S]
autovidedit frames recording.mkv [--every 20 | --at 754.2]
autovidedit plan show|set|apply|options recording.mkv ...
```

Outputs land in `<video>_preprocessed/` next to the input:

| File | Purpose |
|---|---|
| `*_modifications.json` | edit plan; every decision is saved here |
| `*_preview.mp4` | 720p proxy the review UI plays |
| `*_preview.waveform.json` | cached waveform peaks for the UI |
| `*_decisions.json` | last `suggest` output |
| `frames/` | still frames from `frames` / `suggest` |
| `*_edited.mp4` | final render |

### preprocess options

- `--max-pause` longest pause kept, seconds (default 0.5; 0 cuts pauses completely)
- `--silence-threshold` dB level counted as silence (default -40)
- `--min-silence-duration` shortest silence detected, seconds (default 0.5)
- `--padding` trimmed off each end of detected silences, seconds (default 0.1)
- `--no-sentences` skip transcription (silence-only plan)
- `--whisper-model` tiny/base/small/medium/large-v3 (default medium)
- `--force` re-analyze, discarding existing decisions

### suggest options

- `--dry-run` write the decisions file without changing the plan
- `--model` Claude model (default claude-opus-5)
- `--effort` low/medium/high/xhigh/max (default high)
- `--window-minutes` transcript window per request (default 10)
- `--frame-every` seconds between frames sent to Claude (default 20)

### Review UI

- Checkbox = keep in video. Sentences start kept; gaps start removed.
- Click a row to jump there; shift/ctrl-click to multi-select, then **K**eep / **R**emove.
- Badges show who made each decision: **auto** (analyzer), **AI**, or **you**.
  AI and analyzer changes show their reason under the text. **AI changes**
  filters the list to just those rows.
- **Max pause** changes how long pauses may be, and the cut list updates live.
- **Skip removed** (hotkey **S**, on by default) plays the video as it will be cut.
- A yellow bar marks a removed sentence that overlaps kept speech on another
  track; the kept speech wins, so that part stays in.
- Your decisions are never overwritten by a later AI pass.

## Development

```bash
pip install -e .[dev,ai]
pytest
```

Tests generate small synthetic videos with ffmpeg, so no media is checked in.
They include a lip-sync test that cuts a video at many awkward points and
checks every picture change still lines up with its sound.
