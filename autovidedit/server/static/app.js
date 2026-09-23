"use strict";

// The server owns all removal math. Every change is saved immediately and the
// response carries the recomputed removal spans, which drive the waveform,
// the summary and "skip removed" playback.

const KEEP = "keep";
const REMOVE = "remove";
const SPEEDS = [0.5, 1, 1.5, 2, 4, 8];

const state = {
  entries: [],
  duration: 0,
  defaultOutput: "",
  removals: [],
  stats: { count: 0, seconds: 0 },
  rows: [],           // list rows in display order: {entry, el, checkbox}
  selectedIds: new Set(),
  anchorIndex: -1,
  peaks: null,
  skipMode: true,
  aiOnly: false,
  saving: 0,
};

const $ = (id) => document.getElementById(id);
const video = $("video");

function fmtTime(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  const mm = String(m).padStart(2, "0"), ss = String(sec).padStart(2, "0");
  return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

const isRemoved = (e) => e.decision === REMOVE;
const isAiTouched = (e) => e.source === "ai" || (e.source === "analyzer" && e.rationale);

// ---------- data ----------

function applyServerRemovals(data) {
  state.removals = data.removals;
  state.stats = data.stats;
  updateSummary();
  drawWaveform();
}

async function loadState() {
  const res = await fetch("/api/state");
  const data = await res.json();
  state.entries = data.entries;
  state.duration = data.duration;
  state.defaultOutput = data.default_output;
  $("video-name").textContent = data.video_name;
  $("max-pause").value = data.options.max_pause;
  $("time-label").textContent = `${fmtTime(video.currentTime)} / ${fmtTime(state.duration)}`;
  applyServerRemovals(data);
  buildList();
}

async function putPlan(body) {
  state.saving++;
  $("save-status").textContent = "saving…";
  try {
    const res = await fetch("/api/plan", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      alert("Save failed: " + (await res.text()));
      await loadState();
      return;
    }
    applyServerRemovals(await res.json());
  } finally {
    state.saving--;
    $("save-status").textContent = state.saving ? "saving…" : "saved";
  }
}

function setDecisions(entries, decision) {
  const changes = {};
  for (const entry of entries) {
    if (entry.decision === decision && entry.source === "human") continue;
    entry.decision = decision;
    entry.source = "human";
    changes[entry.id] = decision;
  }
  if (Object.keys(changes).length) {
    refreshRows();
    putPlan({ decisions: changes });
  }
}

// ---------- entry list ----------

function describe(entry) {
  const len = (entry.end - entry.start).toFixed(1);
  if (entry.kind === "gap") return { badge: `GAP ${len}s`, text: "(no speech on any track)" };
  if (entry.kind === "cut") return { badge: `CUT ${len}s`, text: entry.rationale || "(cut)" };
  if (entry.kind === "silence") return { badge: `SILENCE ${len}s`, text: "(silence)" };
  return { badge: `Track ${entry.track ?? 1}`, text: entry.text || "" };
}

function buildList() {
  const list = $("entry-list");
  list.innerHTML = "";
  state.rows = [];

  const items = state.entries
    .filter((e) => !state.aiOnly || isAiTouched(e))
    .sort((a, b) => a.start - b.start);

  for (const entry of items) {
    const el = document.createElement("div");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.title = "checked = keep in video";

    const body = document.createElement("div");
    body.className = "body";
    const meta = document.createElement("div");
    meta.className = "meta";
    const words = document.createElement("div");
    words.className = "words";
    const note = document.createElement("div");
    note.className = "note";

    const { badge, text } = describe(entry);
    const time = document.createElement("span");
    time.className = "time";
    time.textContent = `[${fmtTime(entry.start)}]`;
    const kindBadge = document.createElement("span");
    kindBadge.className = "badge";
    kindBadge.textContent = badge;
    const sourceBadge = document.createElement("span");
    sourceBadge.className = "badge source";
    meta.append(time, kindBadge, sourceBadge);
    words.textContent = text;
    body.append(meta, words, note);
    el.append(checkbox, body);
    list.appendChild(el);

    const index = state.rows.length;
    const row = { entry, el, checkbox, sourceBadge, note };
    state.rows.push(row);

    checkbox.addEventListener("click", (ev) => {
      ev.stopPropagation();
      setDecisions([entry], checkbox.checked ? KEEP : REMOVE);
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
    video.currentTime = state.rows[index].entry.start;
  }
  refreshRows();
}

function applyToSelection(decision) {
  const entries = state.rows
    .filter((row) => state.selectedIds.has(row.entry.id))
    .map((row) => row.entry);
  setDecisions(entries, decision);
}

function partlyKeptIds() {
  // A removed sentence overlapping kept speech on another track is only
  // partly cut: kept speech always wins. Flag it so the reviewer knows.
  const sentences = state.entries.filter((e) => e.kind === "sentence");
  const flagged = new Set();
  for (const a of sentences) {
    if (!isRemoved(a)) continue;
    for (const b of sentences) {
      if (b === a || isRemoved(b) || a.track === b.track) continue;
      if (a.start < b.end && b.start < a.end) { flagged.add(a.id); break; }
    }
  }
  return flagged;
}

function refreshRows() {
  const partly = partlyKeptIds();
  for (const { entry, el, checkbox, sourceBadge, note } of state.rows) {
    checkbox.checked = !isRemoved(entry);
    el.className = "entry";
    el.classList.add(entry.kind);
    if (entry.kind === "gap") el.classList.add(isRemoved(entry) ? "removed" : "kept-gap");
    else if (isRemoved(entry)) el.classList.add("removed");
    if (state.selectedIds.has(entry.id)) el.classList.add("selected");
    if (partly.has(entry.id)) {
      el.classList.add("overlap-warn");
      el.title = "Partly kept: overlaps kept speech on another track";
    } else {
      el.title = "";
    }

    const label = { ai: "AI", human: "you", analyzer: entry.rationale ? "auto" : "" }[entry.source];
    sourceBadge.textContent = label || "";
    sourceBadge.className = "badge source " + entry.source;
    sourceBadge.classList.toggle("hidden", !label);
    const confidence = entry.confidence != null ? ` (${Math.round(entry.confidence * 100)}%)` : "";
    note.textContent = entry.kind !== "cut" && entry.rationale ? entry.rationale + confidence : "";
  }
  const n = state.selectedIds.size;
  $("selection-count").textContent = n ? `${n} selected` : "";
}

function updateSummary() {
  const { count, seconds } = state.stats;
  const remaining = state.duration - seconds;
  $("removal-summary").textContent =
    `${count} cut(s), ${seconds.toFixed(1)}s removed — ` +
    `${fmtTime(state.duration)} → ${fmtTime(remaining)}`;
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
    if (ev.target.tagName === "INPUT" && ev.target.type !== "checkbox") return;
    if (ev.code === "Space") { ev.preventDefault(); toggle(); }
    else if (ev.code === "ArrowLeft") video.currentTime = Math.max(0, video.currentTime - 5);
    else if (ev.code === "ArrowRight") video.currentTime = Math.min(state.duration, video.currentTime + 5);
    else if (ev.key === "k" || ev.key === "K") applyToSelection(KEEP);
    else if (ev.key === "r" || ev.key === "R") applyToSelection(REMOVE);
    else if (ev.key === "s" || ev.key === "S") setSkip(!state.skipMode);
  });
}

// Jump past any removed span the playhead enters during playback. Only called
// from timeupdate-while-playing, so manual seeks can land inside removed spans.
function skipRemovedRegion() {
  const t = video.currentTime;
  for (const [start, end] of state.removals) {
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
  const row = state.rows.find(({ entry }) => entry.start <= t && t <= entry.end);
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
  if (!state.peaks || !state.duration) return;
  const { ctx, width, height } = canvasSize();
  ctx.clearRect(0, 0, width, height);

  ctx.fillStyle = "rgba(224, 91, 91, 0.22)";
  for (const [start, end] of state.removals) {
    const x = (start / state.duration) * width;
    const w = Math.max(1, ((end - start) / state.duration) * width);
    ctx.fillRect(x, 0, w, height);
  }

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
    if (!state.removals.length) {
      alert("Nothing is marked for removal - nothing to render.");
      return;
    }
    $("render-summary").textContent =
      `Removing ${state.stats.count} span(s), ${state.stats.seconds.toFixed(1)} seconds total. ` +
      `Output is MP4 (H.264, constant frame rate, every audio track as AAC).`;
    $("render-output").value = state.defaultOutput;
    $("render-form").classList.remove("hidden");
    $("render-progress").classList.add("hidden");
    modal.classList.remove("hidden");
  });

  $("render-cancel").addEventListener("click", () => modal.classList.add("hidden"));
  $("render-close").addEventListener("click", () => modal.classList.add("hidden"));

  $("render-start").addEventListener("click", async () => {
    const res = await fetch("/api/render", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ output: $("render-output").value }),
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
    if (!res.ok && res.status !== 409) alert("Cancel failed: " + (await res.text()));
    $("render-cancel-run").disabled = false;
  });
}

// ---------- init ----------

$("keep-selected").addEventListener("click", () => applyToSelection(KEEP));
$("remove-selected").addEventListener("click", () => applyToSelection(REMOVE));
$("ai-only").addEventListener("click", () => {
  state.aiOnly = !state.aiOnly;
  $("ai-only").classList.toggle("active", state.aiOnly);
  buildList();
});
$("max-pause").addEventListener("change", (ev) => {
  const value = parseFloat(ev.target.value);
  if (!Number.isNaN(value)) putPlan({ max_pause: value });
});
// Pick up plan edits made outside the browser (e.g. `autovidedit plan apply`).
window.addEventListener("focus", () => { if (!state.saving) loadState(); });

setupPlayback();
setupWaveformSeek();
setupRender();
loadState().then(loadWaveform);
