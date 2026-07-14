# Next steps

Recommended follow-ups after the July 2026 revamp (web review UI, faster-whisper,
package restructure), roughly in priority order.

## Done — July 2026 high-priority pass

### 1. "Play as edited" preview mode — DONE
Added a "Skip removed" toggle (hotkey **S**, on by default) in the player
controls. On `timeupdate` while playing, the playhead jumps past any removed
segment (`video.currentTime = end + 0.01`, pausing if that runs off the end).
The removal list is cached in `state.removalSegments` (refreshed by
`refreshRows`) rather than recomputed per frame. Manual seeks still land inside
removed regions. Frontend-only.

### 2. Render cancellation — DONE
End-to-end cancel:
- `ffprobe.run_ffmpeg` gained an optional `cancel_event`; when set it runs
  ffmpeg via `Popen`, polls every 0.2s, and on cancel `terminate()`s (then
  `kill()`s after 3s) and raises `RenderCancelled`. `cancel_event` is threaded
  through `VideoRenderer` and `render_edit_plan`, with a top-of-loop check per
  segment. Temp-dir cleanup already runs in the existing `finally` blocks.
- `POST /api/render/cancel` (409 when idle) sets the event; the render thread
  catches `RenderCancelled` and reports `cancelled: true`. The UI has a Cancel
  button in the progress dialog and shows a neutral "Render cancelled" message.

### 3. Per-track audio bitrate in the final MOV — DONE (documented quirk)
`convert_to_mov` now emits explicit per-stream `-b:a:0 192k -b:a:1 192k …`
(one per mapped audio track) instead of a global `-b:a 192k`, so the 192k
intent is unambiguous.

Root cause of the observed ~128k: ffmpeg's native AAC encoder is ABR, so the
bitrate is an *average target*, not a floor. Trivially compressible content
undershoots it badly — a pure/near-silent sine measures ~57–128k no matter what
`-b:a` says, because the signal simply doesn't need the bits. Verified: a loud,
incompressible track (pink noise) reaches 191991 (~192k), while a quiet tone on
the same run stays ~57k. The per-stream flags do not (and cannot) force CBR on
compressible audio without switching encoders, which we deliberately don't do.
This is expected and acceptable; the explicit flags remove any ambiguity, and
real speech (incompressible enough) lands at the target.

### 4. Server endpoint tests — DONE
Added `tests/test_server.py` (14 tests) using `fastapi.testclient.TestClient`
plus a `fixture_video_2track` session fixture. Covers `/`, `/api/state`, plan
PUT (valid/unknown-id/invalid-value), `/video` full + Range (206) + 416,
waveform shape and caching, render (no-segments 400, 2-track MOV output that
doubles as the Task B bitrate check), cancel-when-idle 409, and cancel
mid-render (asserts `cancelled: true` and no leftover temp dirs). `httpx` added
to the `dev` extra.

## Medium — UX

### 5. Kdenlive / EDL export (chosen stretch feature)
Export the cut list as a Kdenlive project (or EDL) so cuts can be fine-tuned
losslessly in Kdenlive instead of baking them in. Entry point:
`EditPlan.segments_to_remove()` already produces the final cut list; add an
exporter in `core/` plus a button/endpoint.

### 6. Adjustable sentence boundaries in the UI
Whisper timestamps are occasionally off by a few hundred ms. Allow nudging an
entry's start/end (±0.1s buttons or dragging edges on the waveform). Requires a
`PUT /api/plan` extension to accept time edits, not just modification flags.

### 7. Preprocess progress reporting
Transcription and preview generation are the long steps but print nothing
measurable. faster-whisper yields segments as a generator — log progress as
`segment.end / duration`. Same for the preview proxy via `ffmpeg -progress`.

### 8. Undo in the review UI
A simple stack of `{id, previous_modification}` with Ctrl+Z would make bulk
K/R operations safer.

## Low — hygiene & performance

### 9. Merge `revamp` → `main` once trusted
After one real editing session on actual footage, merge and delete the old
branch state. Until then main still has the Qt implementation.

### 10. Clean the working tree of old artifacts
The repo dir still holds ~3.5 GB of old recordings and `*_preprocessed/`
folders from the previous layout (all gitignored). Move recordings to a media
directory now that outputs are written next to the input rather than into the
repo.

### 11. Uninstall orphaned venv packages
`pip uninstall PySide6 python-vlc openai openai-whisper ffmpeg-python pydub`...
(keep pydub — still used). Frees several hundred MB.

### 12. Revisit ffmpeg `silencedetect` if preprocessing feels slow
Deliberately not adopted during the revamp (kept pydub). If silence detection
ever becomes the bottleneck on long recordings, ffmpeg's built-in
`silencedetect` filter does one pass per track with no temp WAV extraction;
`SilenceDetector.detect()` is the only place to swap.

### 13. Waveform cache invalidation
`/api/waveform` caches peaks next to the preview (`*_preview.waveform.json`)
but never checks freshness. If a preview is regenerated, stale peaks are
served. Compare mtimes before using the cache.
