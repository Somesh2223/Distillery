(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);

  let currentMode = "preview";
  let currentRunId = null;
  let pollTimer = null;

  $("mode-preview").addEventListener("click", () => setMode("preview"));
  $("mode-dataset").addEventListener("click", () => setMode("dataset"));
  $("parse-btn").addEventListener("click", onParse);
  $("fetch-btn").addEventListener("click", onFetch);
  $("export-btn").addEventListener("click", onExport);

  function setMode(mode) {
    currentMode = mode;
    $("mode-preview").classList.toggle("active", mode === "preview");
    $("mode-dataset").classList.toggle("active", mode === "dataset");
  }

  function showError(el, message) {
    el.textContent = message;
    el.classList.remove("hidden");
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
        language: $("f-language").value.trim() || null,
        domain_allowlist: domains,
        date_range: (dateFrom || dateTo) ? { from: dateFrom || null, to: dateTo || null } : null,
      },
    };
  }

  async function onFetch() {
    const condition = $("condition").value.trim();
    const structured_query = buildStructuredQuery();
    $("fetch-btn").disabled = true;
    hideError($("fetch-error"));
    $("progress-panel").classList.remove("hidden");
    $("results-panel").classList.add("hidden");
    $("results-grid").innerHTML = "";
    $("progress-label").textContent = "Starting...";
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
        if (run.status === "completed" || run.status === "failed") {
          clearInterval(pollTimer);
          $("fetch-btn").disabled = false;
          if (run.status === "completed") {
            await loadResults();
          } else {
            showError($("fetch-error"), "Fetch failed: " + (run.error || "unknown error"));
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
    $("results-panel").classList.remove("hidden");
    $("results-summary").textContent = `${data.total} item(s) fetched`;
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

  function renderCard(item) {
    const card = document.createElement("div");
    card.className = "card";
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
