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

## Walkthrough

One recording, start to finish. Everything is written next to the input, in
`recording_preprocessed/`; the source file is never modified.

### 1. Analyze

```bash
autovidedit preprocess recording.mkv
```

Transcribes each audio track, finds the silences they share, and writes the
edit plan. This is the slow step: about 9 minutes for a one-hour, two-track
recording on a GPU, and the first run also downloads the Whisper model.

```
== Transcription ==
Loading Whisper model 'medium' (device=auto)...
Transcribing audio track 1/2...
  Found 404 sentence(s) in track 1
Transcribing audio track 2/2...
  Found 233 sentence(s) in track 2
== Silence detection ==
Found 795 silence span(s), 1850.6s where every track is quiet
Marked 11 likely-hallucinated sentence(s) for removal
Edit plan saved to: .../recording_preprocessed/recording_modifications.json
Generating browser preview...
```

Check what it decided before going further:

```bash
autovidedit plan show recording.mkv        # counts, total removed, output duration
autovidedit transcript recording.mkv       # every entry, with its id and decision
```

The transcript is the plan in readable form — this is what you are about to
review:

```
[04b112c3dca4] 0:02.1-0:04.6  KEEP    T1  A toss. Satan spelled backwards.
[48ef42aa3cff] 0:04.8-0:07.6  REMOVE  (gap 2.9s, no speech)
```

### 2. Let Claude propose cuts (optional)

```bash
autovidedit suggest recording.mkv
```

Needs `pip install -e .[ai]` and `ANTHROPIC_API_KEY`. Claude reads the
transcript and looks at frames, then proposes cuts for false starts, setup
chatter, dead stretches and filler words. Each one carries a reason you will
see in the next step. It costs real money on a long recording, so try
`--dry-run` first: that writes `recording_decisions.json` for you to read
without changing the plan.

### 3. Review

```bash
autovidedit review recording.mkv
```

Opens `http://127.0.0.1:8791` (Ctrl+C to stop). The video plays on the left,
the transcript on the right, and **a ticked checkbox means the line stays in**.

Work through it like this:

- **Watch it as it will be cut.** *Skip removed* is on by default, so playback
  jumps over everything marked for removal. This is the fastest way to catch a
  cut that lands badly.
- **Fix what's wrong.** Click a row to jump there. Tick to keep, untick to
  remove; shift or ctrl-click a range, then press **K** or **R**.
- **Check the AI's work.** *AI changes* filters the list to just the rows
  Claude touched, each with its reason underneath. Badges say who decided:
  **auto**, **AI**, or **you**.
- **Tune the pacing.** *Max pause* sets how much silence may remain between
  lines; the cut list and the waveform update as you change it.
- Every change saves immediately — there is no save button, and nothing you
  decide here is ever overwritten by a later `suggest` run.

The footer always shows where you stand: `264 cut(s), 1032.9s removed —
1:00:08 → 42:56`.

### 4. Render

Either press **Render…** in the UI, or:

```bash
autovidedit render recording.mkv
```

One ffmpeg pass writes `recording_preprocessed/recording_edited.mp4` — H.264,
constant frame rate at the source rate, every audio track kept as a separate
AAC stream. With NVENC, a 35-minute output takes about 8 minutes.

```
Removing 620 span(s), 1510.8s total
  [ 10%] Encoding 620 kept span(s), 2098s
  [100%] Rendering complete
```

Drop that file straight into Kdenlive — the tracks arrive separate and in
sync, ready for music, titles and colour.

### Starting over

Re-running `preprocess` on a video that already has a plan is refused, because
the plan holds your review decisions. Pass `--force` to re-analyze and discard
them.

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
autovidedit frames recording.mkv [--every 20 | --at 754.2] [--width 640 --start S --end S]
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
