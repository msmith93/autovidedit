# Editing a video as an agent

Everything the review UI does is also a CLI command, so an agent (Claude
Code, or a script) can edit a video without a browser. The plan file is the
single source of truth; the review UI reloads it whenever it changes on disk,
so an agent and a person can work on the same video at the same time.

## 1. Analyze

```bash
autovidedit preprocess recording.mkv          # transcribe + detect silence + preview
```

Writes `recording_preprocessed/recording_modifications.json`. Pauses longer
than `max_pause` (0.5 s) are already shortened, and likely Whisper
hallucinations are marked for removal with a rationale.

## 2. Read the transcript

```bash
autovidedit transcript recording.mkv                    # whole video
autovidedit transcript recording.mkv --start 600 --end 1200
autovidedit transcript recording.mkv --json --words     # word timings, for filler cuts
```

Each line starts with the entry id you use to change it:

```
[8fa6a2320205] 0:02.1-0:04.6  KEEP    T1  A toss. Satan spelled backwards.
[457f08b78b8d] 0:04.8-0:07.6  REMOVE  (gap 2.9s, no speech)
```

`T1`/`T2` is the audio track (one per speaker). Gaps are spans where nobody
speaks; they are removed by default (down to `max_pause`).

## 3. Look at the video

```bash
autovidedit frames recording.mkv --every 20     # frames/t_000020000.jpg ... + frames.json
autovidedit frames recording.mkv --at 754.2     # one frame at 12:34.2
```

Frames land in `recording_preprocessed/frames/`, named by timestamp in
milliseconds. Read the JPEGs to spot loading screens, idle desktops and other
visually dead stretches.

## 4. Decide

Write a decisions file:

```json
{
  "decisions": [
    {"id": "8fa6a2320205", "decision": "remove",
     "rationale": "false start; retaken at 0:09", "confidence": 0.9}
  ],
  "extra_cuts": [
    {"start": 16.62, "end": 16.95, "rationale": "filler 'um'", "confidence": 0.8}
  ]
}
```

- `decisions` change existing entries (`keep` or `remove`). Only list entries
  whose decision should change.
- `extra_cuts` remove spans that no entry covers exactly: filler words (use
  the word timings from `--json --words`), part of a sentence, a dead stretch.
- Every change needs a rationale; it is shown to the human reviewer.

Apply it:

```bash
autovidedit plan apply recording.mkv decisions.json    # or '-' to read stdin
```

Decisions a human made in the UI are never overridden (they are reported as
`skipped_human`); pass `--force` only if the user asked for that. Unknown ids
reject the whole file without changing anything.

For one-off changes:

```bash
autovidedit plan set recording.mkv 8fa6a2320205 c18e6e7100ec --remove --rationale "setup chatter" --source ai
autovidedit plan options recording.mkv --max-pause 0.8
```

## 5. Check the result

```bash
autovidedit plan show recording.mkv          # counts, removed seconds, output duration
autovidedit plan show recording.mkv --json   # plus every removal span
```

## 6. Hand off or render

```bash
autovidedit review recording.mkv   # human review; "AI changes" filter shows your edits
autovidedit render recording.mkv   # -> recording_preprocessed/recording_edited.mp4
```

## Built-in AI pass

`autovidedit suggest recording.mkv --dry-run` does steps 2-4 with the
Anthropic API (needs `pip install -e .[ai]` and an API key). It writes
`recording_decisions.json`; apply it with `plan apply`, or drop `--dry-run`
to apply directly. Human decisions are protected the same way.

## Python API

The same operations are importable:

```python
from autovidedit.core.project import Project
from autovidedit.core.pipeline import load_plan, render
from autovidedit.core.edit_plan import DecisionSet

project = Project.from_path("recording.mkv")
plan = load_plan(project)
plan.apply_decisions(DecisionSet.model_validate_json(open("decisions.json").read()))
plan.save(project.plan_path)
render(project, plan)
```
