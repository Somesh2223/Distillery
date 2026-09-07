(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);

  let currentMode = "preview";
  let currentRunId = null;
  // The user's stable, originally-desired total — set once when a fetch
  // starts and never changed afterward. Deliberately NOT the same as the
  // server's run.requested_count, which the backend bumps by the full
  // top-up amount each time regardless of how many actually arrive; using
  // that for "how many more do I need" would drift upward on a partial
  // top-up instead of just shrinking the remaining gap.
  let targetCount = 0;
  let pollTimer = null;
  let excludedIds = new Set();
  // Tracks the previously-rendered fetched-count so renderProgress() can
  // animate the rolling-digit counter only when the value actually changes,
  // instead of re-rolling on every 1s poll tick.
  let lastFetchedCount = null;

  $("mode-preview").addEventListener("click", () => setMode("preview"));
  $("mode-dataset").addEventListener("click", () => setMode("dataset"));
  $("parse-btn").addEventListener("click", onParse);
  $("fetch-btn").addEventListener("click", onFetch);
  $("export-btn").addEventListener("click", onExport);
  $("stop-btn").addEventListener("click", onStop);
  $("topup-btn").addEventListener("click", onTopup);

  function setMode(mode) {
    currentMode = mode;
    $("mode-preview").classList.toggle("active", mode === "preview");
    $("mode-dataset").classList.toggle("active", mode === "dataset");
  }

  // Some message boxes (Stitch's amber/mint glass panels) wrap their text in
  // a nested <p> alongside a decorative icon — target that if present so we
  // don't blow away the icon by setting textContent on the container itself.
  function showError(el, message) {
    (el.querySelector("p") || el).textContent = message;
    el.classList.remove("hidden", "info");
    el.classList.add("error");
  }
  function showInfo(el, message) {
    (el.querySelector("p") || el).textContent = message;
    el.classList.remove("hidden", "error");
    el.classList.add("info");
  }
  function hideError(el) {
    el.classList.add("hidden");
  }

  async function onParse() {
    const condition = $("condition").value.trim();
    hideError($("parse-error"));
    if (!condition) {
      showError($("parse-error"), "Type a condition first.");
      return;
    }
    $("parse-btn").disabled = true;
    $("parse-status").textContent = "Parsing with LLM...";
    try {
      const resp = await fetch("/api/parse", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ condition }),
      });
      if (!resp.ok) throw new Error(await resp.text());
      const query = await resp.json();
      populateFilters(query);
      $("filters-panel").classList.remove("hidden");
      $("parse-status").textContent = "Parsed. Review filters below, then start the fetch.";
    } catch (err) {
      showError($("parse-error"), "Failed to parse condition: " + err.message);
      $("parse-status").textContent = "";
    } finally {
      $("parse-btn").disabled = false;
    }
  }

  function populateFilters(q) {
    $("f-data-type").value = q.data_type;
    $("f-count").value = q.count;
    $("f-label").value = q.label || "dataset";
    $("f-keywords").value = (q.keywords || []).join(", ");
    const filters = q.filters || {};
    $("f-resolution").value = filters.resolution || "";
    $("f-orientation").value = filters.orientation || "";
    $("f-language").value = filters.language || "";
    $("f-no-watermark").checked = !!filters.no_watermark;
    $("f-dedupe-across-runs").checked = !!filters.dedupe_across_runs;
    $("f-domains").value = (filters.domain_allowlist || []).join(", ");
    const dr = filters.date_range || {};
    $("f-date-from").value = dr.from || "";
    $("f-date-to").value = dr.to || "";
    setMode(q.output_mode === "dataset" ? "dataset" : "preview");
  }

  function buildStructuredQuery() {
    const keywords = $("f-keywords").value.split(",").map((s) => s.trim()).filter(Boolean);
    const domains = $("f-domains").value.split(",").map((s) => s.trim()).filter(Boolean);
    const dateFrom = $("f-date-from").value.trim();
    const dateTo = $("f-date-to").value.trim();
    return {
      data_type: $("f-data-type").value,
      keywords: keywords,
      count: parseInt($("f-count").value, 10) || 20,
      label: $("f-label").value.trim() || "dataset",
      output_mode: currentMode,
      filters: {
        resolution: $("f-resolution").value || null,
        orientation: $("f-orientation").value || null,
        no_watermark: $("f-no-watermark").checked,
        dedupe_across_runs: $("f-dedupe-across-runs").checked,
        language: $("f-language").value.trim() || null,
        domain_allowlist: domains,
        date_range: (dateFrom || dateTo) ? { from: dateFrom || null, to: dateTo || null } : null,
      },
    };
  }

  async function onFetch() {
    const condition = $("condition").value.trim();
    const structured_query = buildStructuredQuery();
    excludedIds = new Set(); // brand new run — nothing discarded yet
    targetCount = structured_query.count; // fixed for this run's lifetime, including any top-ups
    lastFetchedCount = null; // don't roll from a previous run's count
    $("fetch-btn").disabled = true;
    hideError($("fetch-error"));
    $("progress-panel").classList.remove("hidden");
    $("results-panel").classList.add("hidden");
    $("results-grid").innerHTML = "";
    $("progress-label").textContent = "Starting...";
    const stopBtn = $("stop-btn");
    stopBtn.classList.remove("hidden");
    stopBtn.disabled = false;
    stopBtn.textContent = "Stop fetch";
    try {
      const resp = await fetch("/api/fetch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ condition, structured_query }),
      });
      if (!resp.ok) throw new Error(await resp.text());
      const data = await resp.json();
      currentRunId = data.run_id;
      pollStatus();
    } catch (err) {
      showError($("fetch-error"), "Failed to start fetch: " + err.message);
      $("fetch-btn").disabled = false;
      stopBtn.classList.add("hidden");
    }
  }

  async function onStop() {
    if (!currentRunId) return;
    const stopBtn = $("stop-btn");
    stopBtn.disabled = true;
    stopBtn.textContent = "Stopping...";
    try {
      const resp = await fetch(`/api/runs/${currentRunId}/cancel`, { method: "POST" });
      if (!resp.ok) throw new Error(await resp.text());
    } catch (err) {
      stopBtn.disabled = false;
      stopBtn.textContent = "Stop fetch";
      showError($("fetch-error"), "Failed to stop fetch: " + err.message);
    }
  }

  function pollStatus() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(async () => {
      try {
        const resp = await fetch(`/api/runs/${currentRunId}/status`);
        if (!resp.ok) throw new Error(await resp.text());
        const run = await resp.json();
        renderProgress(run);
        if (run.status === "completed" || run.status === "failed" || run.status === "cancelled") {
          clearInterval(pollTimer);
          $("fetch-btn").disabled = false;
          $("stop-btn").classList.add("hidden");
          if (run.status === "failed") {
            showError($("fetch-error"), "Fetch failed: " + (run.error || "unknown error"));
          } else if (run.status === "cancelled") {
            showInfo($("fetch-error"), `Stopped — ${run.fetched_count} item(s) fetched before you stopped it. Results below.`);
            await loadResults();
          } else {
            await loadResults();
          }
        }
      } catch (err) {
        clearInterval(pollTimer);
        showError($("fetch-error"), "Lost connection while polling status: " + err.message);
      }
    }, 1000);
  }

  function renderProgress(run) {
    const pct = run.requested_count > 0 ? Math.min(100, (run.fetched_count / run.requested_count) * 100) : 0;
    $("progress-bar").style.width = pct + "%";
    $("progress-target").textContent = run.requested_count;
    animateCounter(run.fetched_count);
    $("progress-label").textContent = `Status: ${run.status}`;
  }

  // Rolling-digit counter animation (two stacked spans, translated by one
  // line-height) — only plays when the count actually changed since the
  // last render, so repeated poll ticks at the same value don't re-roll.
  function animateCounter(newVal) {
    const inner = $("counter-inner-current");
    if (lastFetchedCount === null || lastFetchedCount === newVal) {
      inner.style.transition = "none";
      inner.innerHTML = `<span>${newVal}</span><span>${newVal}</span>`;
      inner.style.transform = "translate3d(0,0,0)";
    } else {
      inner.style.transition = "none";
      inner.innerHTML = `<span>${lastFetchedCount}</span><span>${newVal}</span>`;
      inner.style.transform = "translate3d(0,0,0)";
      void inner.offsetWidth; // force reflow so the transition below actually plays
      inner.style.transition = "transform 0.5s cubic-bezier(0.4,0,0.2,1)";
      inner.style.transform = "translate3d(0,-1em,0)";
    }
    lastFetchedCount = newVal;
  }

  async function loadResults() {
    const resp = await fetch(`/api/runs/${currentRunId}/results?limit=200`);
    if (!resp.ok) return;
    const data = await resp.json();
    // NOTE: targetCount is intentionally NOT set from data.run.requested_count
    // here — the server bumps that by the full top-up amount regardless of
    // how many actually arrive, which would drift the user's real target
    // upward on a partial top-up. targetCount is set once in onFetch().
    // NOTE: excludedIds is intentionally NOT reset here — this runs again
    // after a top-up fetch, and previously-discarded items should stay
    // discarded rather than reappearing as kept.
    $("results-panel").classList.remove("hidden");
    updateResultsSummary(data.total);
    if (data.result_hint) {
      if (data.total === 0) {
        showError($("fetch-error"), data.result_hint);
      } else {
        showInfo($("fetch-error"), data.result_hint); // partial shortfall — not an error, just explains "completed" at < requested
      }
    }
    const grid = $("results-grid");
    grid.innerHTML = "";
    for (const item of data.items) {
      grid.appendChild(renderCard(item));
    }
    // Always offer the export/download step once there's something to
    // export — "Quick preview" vs "Build dataset" only ever affected the
    // default count, and hiding the download option behind that toggle
    // left no way to get your results out at all if you'd started in
    // preview mode.
    if (data.total > 0) {
      $("export-section").classList.remove("hidden");
    } else {
      $("export-section").classList.add("hidden");
    }
  }

  function updateResultsSummary(total) {
    const kept = total - excludedIds.size;
    const needed = Math.max(0, targetCount - kept);

    $("kept-count").textContent = `${kept} KEPT`;
    $("discarded-count").textContent = `${excludedIds.size} DISCARDED`;
    $("target-count").textContent = `TARGET ${targetCount}`;
    const pct = targetCount > 0 ? Math.min(100, (kept / targetCount) * 100) : 0;
    $("mini-progress-fill").style.width = pct + "%";

    const topupBtn = $("topup-btn");
    if (needed > 0 && total > 0) {
      $("topup-label").textContent = `FETCH ${needed} MORE`;
      topupBtn.classList.remove("hidden");
    } else {
      topupBtn.classList.add("hidden");
    }
  }

  function fileUrl(itemId) {
    // run_id disambiguates which row/file to serve — the same source URL
    // fetched in two different runs shares an id (dedup is per-run by
    // default now), so without this the server could serve an unrelated
    // run's file, including one that's since been cleaned up.
    return `/api/files/${itemId}?run_id=${encodeURIComponent(currentRunId)}`;
  }

  // `card` here is the inner .glass-card element (Stitch's markup wraps it
  // in a .masonry-item for column layout) — discarded styling and the
  // discard/restore icon+label swap both live on it.
  function setCardDiscarded(card, btn, discarded) {
    card.classList.toggle("discarded", discarded);
    btn.innerHTML = discarded
      ? '<span class="material-symbols-outlined text-[16px]">refresh</span><span class="font-label-caps text-label-caps">RESTORE</span>'
      : '<span class="material-symbols-outlined text-[16px]">close</span><span class="font-label-caps text-label-caps">DISCARD</span>';
    btn.title = discarded
      ? "Restore this item — include it in the dataset export again"
      : "Discard this item — it won't be included in the dataset export";
  }

  function renderCard(item) {
    const wrap = document.createElement("div");
    wrap.className = "masonry-item";
    wrap.dataset.itemId = item.id;

    const card = document.createElement("div");
    card.className = "glass-card rounded-xl overflow-hidden group relative";
    wrap.appendChild(card);

    const media = document.createElement("div");
    media.className = "relative overflow-hidden";
    card.appendChild(media);

    if (item.data_type === "image" && item.local_path) {
      const img = document.createElement("img");
      img.className = "w-full h-auto block object-cover transition-transform duration-700 group-hover:scale-105";
      img.src = fileUrl(item.id);
      img.loading = "lazy";
      media.appendChild(img);
    }

    if (item.local_path) {
      const filename = item.local_path.split("/").pop() || `${item.id}.dat`;
      const downloadLink = document.createElement("a");
      downloadLink.className = "absolute top-3 left-3 p-1.5 rounded-full bg-surface/50 backdrop-blur-md border border-white/10 text-on-surface opacity-0 group-hover:opacity-100 transition-opacity hover:bg-surface/80 flex items-center justify-center";
      downloadLink.href = fileUrl(item.id);
      downloadLink.download = filename;
      downloadLink.title = "Download this item";
      downloadLink.innerHTML = '<span class="material-symbols-outlined text-[18px]">download</span>';
      media.appendChild(downloadLink);
    }

    // Always-visible once discarded (matches Stitch's own "pre-discarded"
    // card example) — otherwise reveals on hover like the download button.
    const discardBtn = document.createElement("button");
    discardBtn.type = "button";
    discardBtn.className = "discard-btn absolute top-3 right-3 px-3 py-1.5 rounded-full bg-surface/50 backdrop-blur-md border border-white/10 text-on-surface flex items-center gap-1 transition-opacity hover:bg-error/20 hover:text-error hover:border-error/30";
    setCardDiscarded(card, discardBtn, excludedIds.has(item.id));
    discardBtn.addEventListener("click", () => {
      const id = wrap.dataset.itemId;
      const nowDiscarded = !excludedIds.has(id);
      if (nowDiscarded) {
        excludedIds.add(id);
      } else {
        excludedIds.delete(id);
      }
      setCardDiscarded(card, discardBtn, nowDiscarded);
      updateResultsSummary($("results-grid").children.length);
    });
    media.appendChild(discardBtn);

    const body = document.createElement("div");
    body.className = "p-4 border-t border-white/5";
    card.appendChild(body);

    const topRow = document.createElement("div");
    topRow.className = "flex justify-between items-center mb-2 gap-2";
    body.appendChild(topRow);

    const code = document.createElement("span");
    code.className = "font-code text-code text-primary";
    code.textContent = "#" + item.id.slice(0, 8);
    topRow.appendChild(code);

    const badge = document.createElement("span");
    badge.className = "font-label-caps text-label-caps text-secondary-fixed bg-secondary-fixed/10 px-2 py-0.5 rounded-full whitespace-nowrap";
    badge.textContent = (item.source_name || "web").toUpperCase();
    topRow.appendChild(badge);

    const caption = document.createElement("p");
    caption.className = "font-body-sm text-body-sm text-on-surface-variant line-clamp-2";
    caption.textContent = item.title || item.text_snippet || item.source_url;
    body.appendChild(caption);

    if (item.data_type !== "image" && item.text_snippet) {
      const snippet = document.createElement("p");
      snippet.className = "font-body-sm text-body-sm text-outline mt-1 line-clamp-3";
      snippet.textContent = item.text_snippet.slice(0, 200);
      body.appendChild(snippet);
    }

    return wrap;
  }

  async function onTopup() {
    if (!currentRunId) return;
    const topupBtn = $("topup-btn");
    const kept = $("results-grid").children.length - excludedIds.size;
    const needed = Math.max(0, targetCount - kept);
    if (needed <= 0) return;

    topupBtn.disabled = true;
    hideError($("fetch-error"));
    $("progress-panel").classList.remove("hidden");
    $("progress-label").textContent = "Starting top-up fetch...";
    const stopBtn = $("stop-btn");
    stopBtn.classList.remove("hidden");
    stopBtn.disabled = false;
    stopBtn.textContent = "Stop fetch";
    try {
      const resp = await fetch(`/api/runs/${currentRunId}/topup`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ count: needed }),
      });
      if (!resp.ok) throw new Error(await resp.text());
      pollStatus();
    } catch (err) {
      showError($("fetch-error"), "Failed to start top-up fetch: " + err.message);
    } finally {
      topupBtn.disabled = false;
    }
  }

  async function onExport() {
    const train = parseFloat($("split-train").value) || 0;
    const val = parseFloat($("split-val").value) || 0;
    const test = parseFloat($("split-test").value) || 0;
    const resizeW = parseInt($("resize-w").value, 10);
    const resizeH = parseInt($("resize-h").value, 10);
    const body = {
      split: { train, val, test },
      seed: 42,
    };
    if (resizeW && resizeH) body.resize = [resizeW, resizeH];
    if (excludedIds.size > 0) body.exclude_ids = Array.from(excludedIds);

    $("export-btn").disabled = true;
    $("export-status").textContent = "Building dataset...";
    try {
      const resp = await fetch(`/api/runs/${currentRunId}/export`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!resp.ok) throw new Error(await resp.text());
      const data = await resp.json();
      $("export-status").textContent = "Dataset built. Downloading...";
      window.location.href = data.download_url;
    } catch (err) {
      $("export-status").textContent = "Export failed: " + err.message;
    } finally {
      $("export-btn").disabled = false;
    }
  }
})();
