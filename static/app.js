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

  function showError(el, message) {
    el.textContent = message;
    el.classList.remove("hidden", "info");
    el.classList.add("error");
  }
  function showInfo(el, message) {
    el.textContent = message;
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
    $("progress-count").textContent = `${run.fetched_count} / ${run.requested_count}`;
    $("progress-label").textContent = `Status: ${run.status}`;
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
    if (data.run.output_mode === "dataset" && data.total > 0) {
      $("export-section").classList.remove("hidden");
    } else {
      $("export-section").classList.add("hidden");
    }
  }

  function updateResultsSummary(total) {
    const kept = total - excludedIds.size;
    const needed = Math.max(0, targetCount - kept);
    $("results-summary").textContent = excludedIds.size > 0
      ? `${kept} of ${total} item(s) kept for export (${excludedIds.size} discarded)`
      : `${total} item(s) fetched`;

    const topupBtn = $("topup-btn");
    if (needed > 0 && total > 0) {
      topupBtn.textContent = `Fetch ${needed} more to reach your target of ${targetCount}`;
      topupBtn.classList.remove("hidden");
    } else {
      topupBtn.classList.add("hidden");
    }
  }

  function setCardDiscarded(card, btn, discarded) {
    card.classList.toggle("discarded", discarded);
    btn.textContent = discarded ? "↺ Restore" : "✕ Discard";
    btn.title = discarded ? "Restore this item — include it in the dataset export again" : "Discard this item — it won't be included in the dataset export";
  }

  function renderCard(item) {
    const card = document.createElement("div");
    card.className = "card";
    card.dataset.itemId = item.id;

    if (item.local_path) {
      const filename = item.local_path.split("/").pop() || `${item.id}.dat`;
      const downloadLink = document.createElement("a");
      downloadLink.className = "download-btn";
      downloadLink.href = `/api/files/${item.id}`;
      downloadLink.download = filename;
      downloadLink.title = "Download this item";
      downloadLink.textContent = "⬇";
      card.appendChild(downloadLink);
    }

    // Always-visible discard control — not just a hint in the summary text,
    // so a first-time user can see at a glance that every card is
    // actionable, without having to already know to click it.
    const discardBtn = document.createElement("button");
    discardBtn.type = "button";
    discardBtn.className = "discard-btn";
    setCardDiscarded(card, discardBtn, excludedIds.has(item.id));
    discardBtn.addEventListener("click", () => {
      const id = card.dataset.itemId;
      const nowDiscarded = !excludedIds.has(id);
      if (nowDiscarded) {
        excludedIds.add(id);
      } else {
        excludedIds.delete(id);
      }
      setCardDiscarded(card, discardBtn, nowDiscarded);
      updateResultsSummary($("results-grid").children.length);
    });
    card.appendChild(discardBtn);

    if (item.data_type === "image" && item.local_path) {
      const img = document.createElement("img");
      img.src = `/api/files/${item.id}`;
      img.loading = "lazy";
      card.appendChild(img);
    }
    const body = document.createElement("div");
    body.className = "body";
    const title = document.createElement("div");
    title.className = "title";
    title.textContent = item.title || item.text_snippet || item.source_url;
    body.appendChild(title);
    if (item.data_type !== "image" && item.text_snippet) {
      const snippet = document.createElement("div");
      snippet.className = "meta";
      snippet.textContent = item.text_snippet.slice(0, 140);
      body.appendChild(snippet);
    }
    const meta = document.createElement("div");
    meta.className = "meta";
    meta.innerHTML = `<span class="badge">${item.source_name || ""}</span>${item.license || "license unknown"}`;
    body.appendChild(meta);
    card.appendChild(body);

    return card;
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
