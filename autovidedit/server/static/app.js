"use strict";

const REMOVED = "REMOVED";
const KEPT = "NONE";
const SPEEDS = [0.5, 1, 1.5, 2, 4, 8];

const state = {
  entries: [],
  duration: 0,
  defaultOutput: "",
  dirty: {},          // id -> modification, not yet saved
  rows: [],           // list rows in display order: {entry, el, checkbox}
  selectedIds: new Set(),
  anchorIndex: -1,
  peaks: null,
  skipMode: true,           // auto-skip removed regions during playback
  removalSegments: [],      // cached segmentsToRemove(), refreshed by refreshRows
};

const $ = (id) => document.getElementById(id);
const video = $("video");

function fmtTime(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  const mm = String(m).padStart(2, "0"), ss = String(sec).padStart(2, "0");
  return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

const isSentence = (e) => e.reason === "Sentence";
const isGap = (e) => e.reason === "gap";
const isSilence = (e) => e.reason === "dead air";
const isRemoved = (e) => e.modification === REMOVED;

// ---------- data ----------

async function loadState() {
  const res = await fetch("/api/state");
  const data = await res.json();
  state.entries = data.entries;
  state.duration = data.duration;
  state.defaultOutput = data.default_output;
  $("video-name").textContent = data.video_name;
  $("time-label").textContent = `${fmtTime(0)} / ${fmtTime(state.duration)}`;
  buildList();
  updateSummary();
}

function setModification(entry, modification) {
  if (entry.modification === modification) return;
  entry.modification = modification;
  state.dirty[entry.id] = modification;
  $("dirty-indicator").classList.remove("hidden");
}

async function save() {
  if (Object.keys(state.dirty).length === 0) return;
  const res = await fetch("/api/plan", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ modifications: state.dirty }),
  });
  if (!res.ok) {
    alert("Save failed: " + (await res.text()));
    return;
  }
  state.dirty = {};
  $("dirty-indicator").classList.add("hidden");
}

// ---------- removal math (mirrors EditPlan.segments_to_remove) ----------

function mergeSegments(segments) {
  if (!segments.length) return [];
  const sorted = [...segments].sort((a, b) => a[0] - b[0]);
  const merged = [sorted[0].slice()];
  for (const [start, end] of sorted.slice(1)) {
    const last = merged[merged.length - 1];
    if (start <= last[1]) last[1] = Math.max(last[1], end);
    else merged.push([start, end]);
  }
  return merged;
}

function segmentsToRemove() {
  const sentences = state.entries.filter(isSentence);
  const range = (e) => [e.start_time, e.end_time];

  if (!sentences.length) {
    return mergeSegments(state.entries.filter(isSilence).filter(isRemoved).map(range));
  }
  const removed = [
    ...sentences.filter(isRemoved).map(range),
    ...state.entries.filter(isGap).filter(isRemoved).map(range),
  ];
  const kept = mergeSegments(sentences.filter((e) => !isRemoved(e)).map(range));
  for (const [sStart, sEnd] of state.entries.filter(isSilence).map(range)) {
    for (const [kStart, kEnd] of kept) {
      const start = Math.max(sStart, kStart), end = Math.min(sEnd, kEnd);
      if (start < end) removed.push([start, end]);
    }
  }
  return mergeSegments(removed);
}

// ---------- entry list ----------

function buildList() {
  const list = $("entry-list");
  list.innerHTML = "";
  state.rows = [];

  const items = state.entries
    .filter((e) => isSentence(e) || isGap(e))
    .sort((a, b) => a.start_time - b.start_time);

  for (const entry of items) {
    const el = document.createElement("div");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = !isRemoved(entry);
    checkbox.title = "checked = keep in video";

    const body = document.createElement("div");
    body.className = "body";
    const meta = document.createElement("div");
    meta.className = "meta";
    const words = document.createElement("div");
    words.className = "words";

    const time = `<span class="time">[${fmtTime(entry.start_time)}]</span>`;
    if (isGap(entry)) {
      const len = (entry.end_time - entry.start_time).toFixed(1);
      meta.innerHTML = `${time}<span class="badge">GAP ${len}s</span>`;
      words.textContent = "(no speech on any track)";
    } else {
      const track = entry.content?.audio_track ?? 1;
      meta.innerHTML = `${time}<span class="badge">Track ${track}</span>`;
      words.textContent = entry.content?.words || "";
    }
    body.append(meta, words);
    el.append(checkbox, body);
    list.appendChild(el);

    const index = state.rows.length;
    const row = { entry, el, checkbox };
    state.rows.push(row);

    checkbox.addEventListener("click", (ev) => {
      ev.stopPropagation();
      setModification(entry, checkbox.checked ? KEPT : REMOVED);
      refreshRows();
    });
    el.addEventListener("click", (ev) => onRowClick(ev, index));
  }
  refreshRows();
}

function onRowClick(ev, index) {
  if (ev.shiftKey && state.anchorIndex >= 0) {
    const [lo, hi] = [Math.min(state.anchorIndex, index), Math.max(state.anchorIndex, index)];
    if (!ev.ctrlKey && !ev.metaKey) state.selectedIds.clear();
    for (let i = lo; i <= hi; i++) state.selectedIds.add(state.rows[i].entry.id);
  } else if (ev.ctrlKey || ev.metaKey) {
    const id = state.rows[index].entry.id;
    state.selectedIds.has(id) ? state.selectedIds.delete(id) : state.selectedIds.add(id);
    state.anchorIndex = index;
  } else {
    state.selectedIds.clear();
    state.selectedIds.add(state.rows[index].entry.id);
    state.anchorIndex = index;
    video.currentTime = state.rows[index].entry.start_time;
  }
  refreshRows();
}

function applyToSelection(modification) {
  for (const row of state.rows) {
    if (state.selectedIds.has(row.entry.id)) {
      setModification(row.entry, modification);
      row.checkbox.checked = modification !== REMOVED;
    }
  }
  refreshRows();
}

function overlapWarnings() {
  // Kept sentences overlapping a REMOVED sentence on another track lose part
  // of their audio context - flag them.
  const sentences = state.entries.filter(isSentence);
  const flagged = new Set();
  for (const a of sentences) {
    if (isRemoved(a)) continue;
    for (const b of sentences) {
      if (b === a || !isRemoved(b)) continue;
      if ((a.content?.audio_track) === (b.content?.audio_track)) continue;
      if (a.start_time < b.end_time && b.start_time < a.end_time) {
        flagged.add(a.id);
        break;
      }
    }
  }
  return flagged;
}

function refreshRows() {
  state.removalSegments = segmentsToRemove();
  const warnings = overlapWarnings();
  for (const { entry, el } of state.rows) {
    el.className = "entry";
    if (isGap(entry)) {
      el.classList.add("gap", isRemoved(entry) ? "removed" : "kept-gap");
    } else if (isRemoved(entry)) {
      el.classList.add("removed");
    }
    if (state.selectedIds.has(entry.id)) el.classList.add("selected");
    if (warnings.has(entry.id)) el.classList.add("overlap-warn");
  }
  const n = state.selectedIds.size;
  $("selection-count").textContent = n ? `${n} selected` : "";
  updateSummary();
  drawWaveform();
}

function updateSummary() {
  const segments = segmentsToRemove();
  const total = segments.reduce((acc, [s, e]) => acc + (e - s), 0);
  $("removal-summary").textContent =
    `${segments.length} segment(s) to remove - ${total.toFixed(1)}s of ${state.duration.toFixed(1)}s`;
}

// ---------- playback ----------

function setupPlayback() {
  const playBtn = $("play-btn");
  const toggle = () => (video.paused ? video.play() : video.pause());
  playBtn.addEventListener("click", toggle);
  video.addEventListener("click", toggle);
  video.addEventListener("play", () => (playBtn.textContent = "⏸"));
  video.addEventListener("pause", () => (playBtn.textContent = "▶"));

  const speedBox = $("speed-buttons");
  for (const speed of SPEEDS) {
    const btn = document.createElement("button");
    btn.textContent = speed + "x";
    if (speed === 1) btn.classList.add("active");
    btn.addEventListener("click", () => {
      video.playbackRate = speed;
      speedBox.querySelectorAll("button").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
    });
    speedBox.appendChild(btn);
  }

  const skipBtn = $("skip-btn");
  const setSkip = (on) => {
    state.skipMode = on;
    skipBtn.classList.toggle("active", on);
  };
  skipBtn.addEventListener("click", () => setSkip(!state.skipMode));

  video.addEventListener("timeupdate", () => {
    if (state.skipMode && !video.paused) skipRemovedRegion();
    $("time-label").textContent = `${fmtTime(video.currentTime)} / ${fmtTime(state.duration)}`;
    highlightPlayingRow();
    drawPlayhead();
  });

  document.addEventListener("keydown", (ev) => {
    if (ev.target.tagName === "INPUT" && ev.target.type === "text") return;
    if (ev.code === "Space") { ev.preventDefault(); toggle(); }
    else if (ev.code === "ArrowLeft") video.currentTime = Math.max(0, video.currentTime - 5);
    else if (ev.code === "ArrowRight") video.currentTime = Math.min(state.duration, video.currentTime + 5);
    else if (ev.key === "k" || ev.key === "K") applyToSelection(KEPT);
    else if (ev.key === "r" || ev.key === "R") applyToSelection(REMOVED);
    else if (ev.key === "s" || ev.key === "S") setSkip(!state.skipMode);
  });
}

// Jump the playhead past any removed segment it has entered during playback.
// Only invoked from timeupdate-while-playing, so manual seeks can still land
// inside removed regions for inspection.
function skipRemovedRegion() {
  const t = video.currentTime;
  for (const [start, end] of state.removalSegments) {
    if (start <= t && t < end) {
      if (end + 0.01 >= state.duration) video.pause();
      else video.currentTime = end + 0.01;
      return;
    }
  }
}

let lastPlayingRow = null;
function highlightPlayingRow() {
  const t = video.currentTime;
  const row = state.rows.find(
    ({ entry }) => entry.start_time <= t && t <= entry.end_time
  );
  if (row === lastPlayingRow) return;
  lastPlayingRow?.el.classList.remove("playing");
  lastPlayingRow = row || null;
  if (row) {
    row.el.classList.add("playing");
    row.el.scrollIntoView({ block: "nearest" });
  }
}

// ---------- waveform ----------

async function loadWaveform() {
  const res = await fetch("/api/waveform");
  state.peaks = (await res.json()).peaks;
  drawWaveform();
}

function canvasSize() {
  const canvas = $("waveform");
  const dpr = window.devicePixelRatio || 1;
  const width = canvas.clientWidth, height = canvas.clientHeight;
  if (canvas.width !== width * dpr) {
    canvas.width = width * dpr;
    canvas.height = height * dpr;
  }
  return { canvas, ctx: canvas.getContext("2d"), width: canvas.width, height: canvas.height };
}

function drawWaveform() {
  if (!state.peaks) return;
  const { ctx, width, height } = canvasSize();
  ctx.clearRect(0, 0, width, height);

  // removed regions
  ctx.fillStyle = "rgba(224, 91, 91, 0.22)";
  for (const [start, end] of segmentsToRemove()) {
    const x = (start / state.duration) * width;
    const w = ((end - start) / state.duration) * width;
    ctx.fillRect(x, 0, w, height);
  }

  // peaks
  ctx.fillStyle = "#4d9fff";
  const mid = height / 2;
  const barWidth = width / state.peaks.length;
  for (let i = 0; i < state.peaks.length; i++) {
    const h = Math.max(1, state.peaks[i] * (height * 0.92));
    ctx.fillRect(i * barWidth, mid - h / 2, Math.max(1, barWidth * 0.7), h);
  }
  drawPlayhead(true);
}

function drawPlayhead(skipRedraw) {
  if (!state.peaks || !state.duration) return;
  if (!skipRedraw) return drawWaveform();
  const { ctx, width, height } = canvasSize();
  const x = (video.currentTime / state.duration) * width;
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(x, 0, 2, height);
}

function setupWaveformSeek() {
  $("waveform").addEventListener("click", (ev) => {
    const rect = ev.currentTarget.getBoundingClientRect();
    const frac = (ev.clientX - rect.left) / rect.width;
    video.currentTime = frac * state.duration;
  });
  window.addEventListener("resize", drawWaveform);
}

// ---------- render modal ----------

function setupRender() {
  const modal = $("render-modal");

  $("render-btn").addEventListener("click", () => {
    const segments = segmentsToRemove();
    if (!segments.length) {
      alert("No segments are marked for removal - nothing to render.");
      return;
    }
    const total = segments.reduce((acc, [s, e]) => acc + (e - s), 0);
    $("render-summary").textContent =
      `Removing ${segments.length} segment(s), ${total.toFixed(1)} seconds total.` +
      (Object.keys(state.dirty).length ? " Unsaved changes will be saved first." : "");
    $("render-output").value = state.defaultOutput;
    $("render-form").classList.remove("hidden");
    $("render-progress").classList.add("hidden");
    modal.classList.remove("hidden");
  });

  $("render-cancel").addEventListener("click", () => modal.classList.add("hidden"));
  $("render-close").addEventListener("click", () => modal.classList.add("hidden"));

  $("render-start").addEventListener("click", async () => {
    await save();
    const res = await fetch("/api/render", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        output: $("render-output").value,
        re_encode: $("render-reencode").checked,
      }),
    });
    if (!res.ok) {
      alert("Render failed to start: " + (await res.text()));
      return;
    }
    $("render-form").classList.add("hidden");
    $("render-progress").classList.remove("hidden");
    $("render-close").classList.add("hidden");
    $("render-cancel-run").classList.remove("hidden");
    $("progress-msg").className = "";

    const events = new EventSource("/api/render/progress");
    events.onmessage = (ev) => {
      const data = JSON.parse(ev.data);
      $("progress-fill").style.width = data.pct + "%";
      $("progress-msg").textContent = data.msg || "";
      if (data.done) {
        events.close();
        $("render-cancel-run").classList.add("hidden");
        $("render-close").classList.remove("hidden");
        if (data.success) {
          $("progress-msg").textContent = "Done! Saved to: " + data.output;
          $("progress-msg").className = "success";
        } else if (data.cancelled) {
          $("progress-msg").textContent = "Render cancelled.";
          $("progress-msg").className = "";
        } else {
          $("progress-msg").textContent = "Render failed: " + data.error;
          $("progress-msg").className = "error";
        }
      }
    };
  });

  $("render-cancel-run").addEventListener("click", async () => {
    $("render-cancel-run").disabled = true;
    const res = await fetch("/api/render/cancel", { method: "POST" });
    if (!res.ok && res.status !== 409) {
      alert("Cancel failed: " + (await res.text()));
    }
    $("render-cancel-run").disabled = false;
  });
}

// ---------- init ----------

$("save-btn").addEventListener("click", save);
$("keep-selected").addEventListener("click", () => applyToSelection(KEPT));
$("remove-selected").addEventListener("click", () => applyToSelection(REMOVED));

window.addEventListener("beforeunload", (ev) => {
  if (Object.keys(state.dirty).length) ev.preventDefault();
});

setupPlayback();
setupWaveformSeek();
setupRender();
loadState().then(loadWaveform);
