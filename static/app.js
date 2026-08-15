const $ = (id) => document.getElementById(id);
const qs = (sel, root = document) => root.querySelector(sel);
const qsa = (sel, root = document) => [...root.querySelectorAll(sel)];

let currentRunId = null;
let pollTimer = null;
let currentVideoId = null;
let currentFormat = "prose";

// ---------------------------------------------------------------------------
// Toast
// ---------------------------------------------------------------------------

let toastTimer = null;
function toast(msg) {
  const el = $("toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 2200);
}

// ---------------------------------------------------------------------------
// Advanced options toggle
// ---------------------------------------------------------------------------

$("toggle-advanced").addEventListener("click", () => {
  const el = $("advanced-options");
  const hidden = el.classList.toggle("hidden");
  $("toggle-advanced").textContent = hidden ? "More options" : "Fewer options";
});

// ---------------------------------------------------------------------------
// Cue-time formatting (elapsed seconds -> caption-style range)
// ---------------------------------------------------------------------------

function cueTimeLabel(atSeconds) {
  const fmt = (s) => {
    const ms = Math.round((s % 1) * 1000);
    const total = Math.floor(s);
    const hh = String(Math.floor(total / 3600)).padStart(2, "0");
    const mm = String(Math.floor((total % 3600) / 60)).padStart(2, "0");
    const ss = String(total % 60).padStart(2, "0");
    return `${hh}:${mm}:${ss}.${String(ms).padStart(3, "0")}`;
  };
  const end = atSeconds + 1.2;
  return `${fmt(atSeconds)} --> ${fmt(end)}`;
}

function escapeHtml(s) {
  const d = document.createElement("div");
  d.textContent = s == null ? "" : String(s);
  return d.innerHTML;
}

// ---------------------------------------------------------------------------
// Cue log rendering
// ---------------------------------------------------------------------------

let renderedCueCount = 0;

function renderCueLog(log) {
  const el = $("cue-log");
  if (!log || log.length === 0) {
    if (renderedCueCount !== 0) return;
    el.innerHTML = `<div class="cue-log-empty">waiting for a channel URL</div>`;
    return;
  }
  if (log.length === renderedCueCount) return;

  const wasNearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
  el.innerHTML = log.map((c) => `
    <div class="cue kind-${c.kind}">
      <div class="cue-time">${cueTimeLabel(c.at)}</div>
      <div class="cue-text">${escapeHtml(c.text)}</div>
    </div>
  `).join("");
  renderedCueCount = log.length;
  if (wasNearBottom || renderedCueCount === log.length) {
    el.scrollTop = el.scrollHeight;
  }
}

// ---------------------------------------------------------------------------
// Submitting a run
// ---------------------------------------------------------------------------

$("run-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const url = $("url").value.trim();
  const hint = $("input-hint");
  if (!url) {
    hint.textContent = "Paste a channel, playlist, or video URL first.";
    hint.classList.add("error");
    return;
  }
  hint.textContent = "";
  hint.classList.remove("error");
  $("fetch-btn").disabled = true;

  const body = {
    url,
    lang: $("opt-lang").value.trim() || "en",
    auto: $("opt-auto").checked,
    shorts: $("opt-shorts").checked,
    limit: parseInt($("opt-limit").value || "0", 10),
    workers: parseInt($("opt-workers").value || "3", 10),
    pause: parseFloat($("opt-pause").value || "0.6"),
    browser: $("opt-browser").value || null,
    bundle_chars: parseInt($("opt-bundle-chars").value || "300000", 10),
    timestamps: $("opt-timestamps").checked,
  };

  try {
    const res = await fetch("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Could not start that run.");
    startWatchingRun(data.run_id);
  } catch (err) {
    hint.textContent = err.message;
    hint.classList.add("error");
  } finally {
    $("fetch-btn").disabled = false;
  }
});

function startWatchingRun(runId) {
  currentRunId = runId;
  renderedCueCount = 0;
  $("log-panel").classList.remove("hidden");
  $("stats-panel").classList.add("hidden");
  $("results-panel").classList.add("hidden");
  $("cue-log").innerHTML = "";
  $("cancel-btn").disabled = false;
  $("cancel-btn").textContent = "Cancel";
  clearInterval(pollTimer);
  pollTimer = setInterval(pollRun, 700);
  pollRun();
}

async function pollRun() {
  if (!currentRunId) return;
  let res, job;
  try {
    res = await fetch(`/api/runs/${currentRunId}`);
    job = await res.json();
  } catch {
    return;
  }
  if (!res.ok) {
    clearInterval(pollTimer);
    return;
  }

  renderCueLog(job.log);
  const pct = job.total ? Math.round((job.done / job.total) * 100) : 0;
  $("progress-fill").style.width = `${pct}%`;
  $("progress-text").textContent = `${job.done} / ${job.total}`;
  $("log-heading").textContent = job.status === "running" ? job.stage : job.status;

  if (job.status !== "running") {
    clearInterval(pollTimer);
    $("cancel-btn").disabled = true;
    if (job.error) {
      $("input-hint").textContent = job.error;
      $("input-hint").classList.add("error");
    }
    showResults(job);
    refreshRunChips();
  }
}

$("cancel-btn").addEventListener("click", async () => {
  if (!currentRunId) return;
  $("cancel-btn").disabled = true;
  $("cancel-btn").textContent = "Cancelling…";
  await fetch(`/api/runs/${currentRunId}/cancel`, { method: "POST" });
});

// ---------------------------------------------------------------------------
// Results: stats, transcripts table, bundles, search
// ---------------------------------------------------------------------------

function showResults(job) {
  $("stats-panel").classList.remove("hidden");
  $("results-panel").classList.remove("hidden");

  const stats = job.stats || {};
  $("stat-videos").textContent = stats.videos ?? job.videos.length;
  $("stat-captioned").textContent = stats.captioned ?? "–";
  $("stat-words").textContent = (stats.words ?? 0).toLocaleString();
  $("stat-tokens").textContent = (stats.tokens ?? 0).toLocaleString();
  $("stat-bundles").textContent = (stats.bundles || []).length;

  renderVideoTable(job.videos);
  renderBundles(job.bundles || []);
}

function renderVideoTable(videos) {
  const body = $("video-table-body");
  body.innerHTML = "";
  for (const v of videos) {
    const tr = document.createElement("tr");
    const clickable = v.status === "ok";
    if (clickable) tr.classList.add("clickable");
    tr.innerHTML = `
      <td class="title-cell">${escapeHtml(v.title)}</td>
      <td class="mono">${formatUploadDate(v.uploaded)}</td>
      <td class="mono">${formatDuration(v.duration)}</td>
      <td class="mono">${v.words ? v.words.toLocaleString() : "–"}</td>
      <td><span class="status-pill ${v.status}" title="${escapeHtml(v.note || "")}">${v.status.replace("-", " ")}</span></td>
    `;
    if (clickable) {
      tr.addEventListener("click", () => openTranscript(v.video_id, v.title));
    }
    body.appendChild(tr);
  }
}

function formatUploadDate(d) {
  if (!d || String(d).length !== 8) return "–";
  const s = String(d);
  return `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)}`;
}

function formatDuration(sec) {
  if (!sec) return "–";
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

function renderBundles(bundles) {
  const grid = $("bundle-grid");
  grid.innerHTML = "";
  if (bundles.length === 0) {
    grid.innerHTML = `<p class="search-empty">No captioned videos made it into a bundle.</p>`;
    return;
  }
  for (const b of bundles) {
    const card = document.createElement("div");
    card.className = "bundle-card";
    card.innerHTML = `
      <div class="bundle-n">Bundle ${b.n}</div>
      <div class="bundle-meta">${b.videos} video${b.videos === 1 ? "" : "s"} · ${(b.chars / 1024).toFixed(0)} KB · ~${b.tokens.toLocaleString()} est. tokens</div>
      <div class="bundle-actions">
        <button class="btn btn-primary btn-sm" data-action="copy" data-n="${b.n}" data-tokens="${b.tokens}">Copy</button>
        <button class="btn btn-ghost btn-sm" data-action="download" data-n="${b.n}">Download</button>
      </div>
    `;
    grid.appendChild(card);
  }
}

$("bundle-grid").addEventListener("click", async (e) => {
  const btn = e.target.closest("button");
  if (!btn) return;
  const n = btn.dataset.n;
  const url = `/api/runs/${currentRunId}/bundle/${n}`;
  if (btn.dataset.action === "copy") {
    const text = await (await fetch(url)).text();
    await navigator.clipboard.writeText(text);
    toast(`Copied — ~${parseInt(btn.dataset.tokens, 10).toLocaleString()} est. tokens`);
  } else {
    const a = document.createElement("a");
    a.href = url;
    a.download = `bundle-${String(n).padStart(2, "0")}.txt`;
    a.click();
  }
});

$("download-zip-btn").addEventListener("click", () => {
  if (!currentRunId) return;
  window.location.href = `/api/runs/${currentRunId}/zip`;
});

// ---------------------------------------------------------------------------
// Tabs
// ---------------------------------------------------------------------------

qsa(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    qsa(".tab").forEach((t) => t.classList.remove("active"));
    tab.classList.add("active");
    qsa(".tab-panel").forEach((p) => p.classList.add("hidden"));
    $(`tab-${tab.dataset.tab}`).classList.remove("hidden");
  });
});

// ---------------------------------------------------------------------------
// Transcript panel
// ---------------------------------------------------------------------------

async function openTranscript(videoId, title) {
  currentVideoId = videoId;
  $("transcript-title").textContent = title;
  $("transcript-panel-wrap").style.display = "block";
  qsa(".format-btn").forEach((b) => b.classList.toggle("active", b.dataset.format === currentFormat));
  await loadTranscriptBody();
  $("transcript-panel-wrap").scrollIntoView({ behavior: "smooth", block: "start" });
}

async function loadTranscriptBody() {
  const res = await fetch(`/api/runs/${currentRunId}/video/${currentVideoId}?format=${currentFormat}`);
  const text = res.ok ? await res.text() : "Could not load this transcript.";
  $("transcript-body").textContent = text;
}

qsa(".format-btn").forEach((b) => {
  b.addEventListener("click", async () => {
    currentFormat = b.dataset.format;
    qsa(".format-btn").forEach((x) => x.classList.toggle("active", x === b));
    await loadTranscriptBody();
  });
});

$("close-transcript").addEventListener("click", () => {
  $("transcript-panel-wrap").style.display = "none";
});

// ---------------------------------------------------------------------------
// Search
// ---------------------------------------------------------------------------

let searchDebounce = null;
$("search-input").addEventListener("input", () => {
  clearTimeout(searchDebounce);
  searchDebounce = setTimeout(runSearch, 250);
});
$("search-all-runs").addEventListener("change", runSearch);

async function runSearch() {
  const q = $("search-input").value.trim();
  const container = $("search-results");
  if (!q) {
    container.innerHTML = `<p class="search-empty">type a word or phrase to search everything this channel said</p>`;
    return;
  }
  const scopeAll = $("search-all-runs").checked;
  const params = new URLSearchParams({ q });
  if (!scopeAll && currentRunId) params.set("run_id", currentRunId);

  const res = await fetch(`/api/search?${params.toString()}`);
  const results = res.ok ? await res.json() : [];

  if (results.length === 0) {
    container.innerHTML = `<p class="search-empty">no matches</p>`;
    return;
  }
  container.innerHTML = results.map((r) => `
    <div class="search-hit">
      <div class="hit-time">${cueTimeLabelPlain(r.at)}</div>
      <div class="hit-excerpt">${r.excerpt}</div>
      <div class="hit-footer">
        <span class="hit-title">${escapeHtml(r.title)}</span>
        <a class="hit-link" href="${r.url}" target="_blank" rel="noopener">watch at ${formatDuration(Math.round(r.at))} →</a>
      </div>
    </div>
  `).join("");
}

function cueTimeLabelPlain(at) {
  const total = Math.floor(at || 0);
  const hh = String(Math.floor(total / 3600)).padStart(2, "0");
  const mm = String(Math.floor((total % 3600) / 60)).padStart(2, "0");
  const ss = String(total % 60).padStart(2, "0");
  return `${hh}:${mm}:${ss}`;
}

// ---------------------------------------------------------------------------
// Past runs
// ---------------------------------------------------------------------------

async function refreshRunChips() {
  const res = await fetch("/api/runs");
  const runs = res.ok ? await res.json() : [];
  const el = $("run-chips");
  if (runs.length === 0) {
    el.innerHTML = `<p class="search-empty">no runs yet</p>`;
    return;
  }
  el.innerHTML = runs.map((r) => `
    <div class="run-chip" data-run-id="${r.run_id}">
      <span class="chip-status ${r.status}"></span>
      <span>${escapeHtml(r.source || r.url)}</span>
      <button class="chip-delete" data-delete="${r.run_id}" title="Delete this run">×</button>
    </div>
  `).join("");

  qsa(".run-chip").forEach((chip) => {
    chip.addEventListener("click", (e) => {
      if (e.target.closest(".chip-delete")) return;
      startWatchingRun(chip.dataset.runId);
    });
  });
  qsa("[data-delete]").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const id = btn.dataset.delete;
      await fetch(`/api/runs/${id}`, { method: "DELETE" });
      if (id === currentRunId) {
        currentRunId = null;
        $("log-panel").classList.add("hidden");
        $("stats-panel").classList.add("hidden");
        $("results-panel").classList.add("hidden");
        $("transcript-panel-wrap").style.display = "none";
      }
      refreshRunChips();
    });
  });
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

renderCueLog([]);
refreshRunChips();
