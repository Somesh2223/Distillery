"""FastAPI app: submit a natural-language condition, poll fetch progress,
preview results, and export/download a dataset zip. Serves the single-page
UI from static/.

Run with: uvicorn app:app --reload
"""
from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import dataset_export
import query_parser
import source_router
import storage
from config import BASE_DIR, FETCHED_DIR, GOOGLE_CSE_API_KEY, GOOGLE_CSE_CX
from connectors.base import ConnectorUnavailableError
from logging_setup import configure_logging, get_logger
from models import ExportOptions, StructuredQuery

configure_logging()
logger = get_logger("app")

app = FastAPI(title="Distillery", description="Distill web data into labeled, ML-ready datasets from a plain-English condition.")


class ParseRequest(BaseModel):
    condition: str


class FetchRequest(BaseModel):
    condition: str
    structured_query: StructuredQuery


class ExportRequest(BaseModel):
    split: Optional[dict] = None
    resize: Optional[list[int]] = None
    seed: int = 42
    exclude_ids: Optional[list[str]] = None


@app.post("/api/parse")
def parse_condition(req: ParseRequest):
    if not req.condition.strip():
        raise HTTPException(400, "condition must not be empty")
    query = query_parser.parse_condition(req.condition)
    return query.model_dump(by_alias=True)


_CANCEL_EVENTS: dict[str, threading.Event] = {}


def _run_fetch_job(
    run_id: str,
    query: StructuredQuery,
    cancel_event: threading.Event,
    existing_count: int = 0,
    target_new: Optional[int] = None,
) -> None:
    storage.update_run_status(run_id, "running")
    try:
        source_router.route(query, run_id, cancel_event=cancel_event, existing_count=existing_count, target_new=target_new)
        storage.update_run_status(run_id, "cancelled" if cancel_event.is_set() else "completed")
    except ConnectorUnavailableError as exc:
        # An expected, already-classified failure (no internet, bad API key,
        # rate limit, upstream outage) — log it plainly, not as a crash.
        logger.warning("fetch job for run %s failed: [%s] %s", run_id, exc.category, exc)
        storage.update_run_status(run_id, "failed", error=str(exc))
    except Exception as exc:
        logger.exception("fetch job failed for run %s", run_id)
        storage.update_run_status(run_id, "failed", error=str(exc))
    finally:
        _CANCEL_EVENTS.pop(run_id, None)


@app.post("/api/fetch")
def start_fetch(req: FetchRequest):
    run_id = uuid.uuid4().hex[:16]
    storage.create_run(
        run_id,
        req.condition,
        req.structured_query.model_dump(by_alias=True),
        req.structured_query.count,
        req.structured_query.output_mode,
    )
    cancel_event = threading.Event()
    _CANCEL_EVENTS[run_id] = cancel_event
    thread = threading.Thread(target=_run_fetch_job, args=(run_id, req.structured_query, cancel_event), daemon=True)
    thread.start()
    return {"run_id": run_id}


@app.post("/api/runs/{run_id}/cancel")
def cancel_run(run_id: str):
    run = storage.get_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    cancel_event = _CANCEL_EVENTS.get(run_id)
    if cancel_event is None:
        raise HTTPException(409, f"run is already '{run['status']}' — nothing to stop")
    cancel_event.set()
    return {"status": "cancelling"}


class TopupRequest(BaseModel):
    count: int


@app.post("/api/runs/{run_id}/topup")
def topup_run(run_id: str, req: TopupRequest):
    """Fetches `count` more items on top of what a run already has — used
    when discarding items (or a partial shortfall) leaves you with fewer
    kept items than you originally asked for."""
    run = storage.get_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    if run["status"] not in ("completed", "cancelled", "failed"):
        raise HTTPException(409, f"run is '{run['status']}' — wait for it to finish before topping up")
    if req.count <= 0:
        raise HTTPException(400, "count must be positive")

    structured = json.loads(run["structured_query"])
    base_query = StructuredQuery.model_validate(structured)
    baseline = storage.count_items_for_run(run_id)
    # Ask connectors for a candidate pool covering both what's already fetched
    # and the new amount — requesting exactly `req.count` would just re-return
    # the same top-ranked (already-duplicate) results for the same query.
    topup_query = base_query.model_copy(update={"count": baseline + req.count})
    storage.set_run_requested_count(run_id, run["requested_count"] + req.count)

    cancel_event = threading.Event()
    _CANCEL_EVENTS[run_id] = cancel_event
    thread = threading.Thread(
        target=_run_fetch_job,
        args=(run_id, topup_query, cancel_event, baseline, req.count),
        daemon=True,
    )
    thread.start()
    return {"run_id": run_id, "topping_up": req.count}


@app.get("/api/runs/{run_id}/status")
def run_status(run_id: str):
    run = storage.get_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    return run


_DATA_TYPE_KEY_HINTS = {
    "image": "UNSPLASH_ACCESS_KEY, PEXELS_API_KEY, or PIXABAY_API_KEY",
    "text": "NEWSAPI_KEY or REDDIT_CLIENT_ID/REDDIT_CLIENT_SECRET (Wikipedia/Hacker News need no key but may not cover your topic)",
    "structured": "no key needed for Wikipedia tables — try rephrasing to match a specific Wikipedia list article",
}


def _zero_result_hint(data_type: str) -> str:
    configured = [c.name for c in source_router.CONNECTORS_BY_TYPE.get(data_type, []) if c.is_configured()]
    key_hint = _DATA_TYPE_KEY_HINTS.get(data_type, "an API key for this data type")
    if configured:
        return (
            f"No results came back from {', '.join(configured)} or the scraper fallback for this query. "
            "Try broader/fewer keywords, or add filters.domain_allowlist so the scraper can crawl a specific site."
        )
    search_configured = bool(GOOGLE_CSE_API_KEY and GOOGLE_CSE_CX)
    if search_configured:
        return f"No {data_type} API connector is configured ({key_hint}), and the scraper fallback found no matches either."
    return (
        f"No {data_type} API connector is configured ({key_hint}), and the scraper fallback has no way to discover "
        "URLs across the web without GOOGLE_CSE_API_KEY + GOOGLE_CSE_CX (or a filters.domain_allowlist to crawl a "
        "specific site directly). See README.md for free-tier key sources — this is why you're seeing 0 results."
    )


def _shortfall_hint(data_type: str, fetched: int, requested: int) -> str:
    configured = [c.name for c in source_router.CONNECTORS_BY_TYPE.get(data_type, []) if c.is_configured()]
    source_desc = ", ".join(configured) if configured else "the configured connector(s)"
    return (
        f"Only found {fetched} of the {requested} requested — {source_desc} and the scraper fallback ran out of "
        "distinct matches for these keywords after deduping. This is a real stopping point, not a bug: try "
        "broader/fewer keywords, a lower count, or (for images) additional API keys so more sources can contribute."
    )


@app.get("/api/runs/{run_id}/results")
def run_results(run_id: str, offset: int = 0, limit: int = 60):
    run = storage.get_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    items = storage.list_items_for_run(run_id, offset, limit)
    total = storage.count_items_for_run(run_id)
    hint = None
    if run["status"] == "completed":
        structured = json.loads(run["structured_query"])
        data_type = structured.get("data_type", "text")
        if total == 0:
            hint = _zero_result_hint(data_type)
        elif total < run["requested_count"]:
            hint = _shortfall_hint(data_type, total, run["requested_count"])
    return {"items": items, "total": total, "run": run, "result_hint": hint}


@app.get("/api/files/{item_id}")
def get_file(item_id: str, run_id: Optional[str] = None):
    # run_id disambiguates which row to serve — since dedup is per-run by
    # default, the same source URL fetched in two different runs produces
    # two rows sharing this id, potentially pointing at different files (or
    # one whose file has since been cleaned up). Always pass it when known.
    item = storage.get_item(item_id, run_id=run_id)
    if not item or not item["local_path"]:
        raise HTTPException(404, "file not found")
    path = FETCHED_DIR / item["local_path"]
    if not path.exists():
        raise HTTPException(404, "file missing on disk")
    return FileResponse(path)


@app.post("/api/runs/{run_id}/export")
def export_run(run_id: str, req: ExportRequest):
    run = storage.get_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    structured = json.loads(run["structured_query"])

    kwargs: dict = {"seed": req.seed}
    if req.split:
        kwargs["split"] = req.split
    if req.resize:
        kwargs["resize"] = tuple(req.resize)
    options = ExportOptions(**kwargs)

    exclude_ids = set(req.exclude_ids) if req.exclude_ids else None
    try:
        zip_path = dataset_export.export_dataset(
            run_id, structured.get("label", "dataset"), structured.get("data_type", "text"), options,
            exclude_ids=exclude_ids,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"download_url": f"/api/runs/{run_id}/download", "zip_path": str(zip_path)}


@app.get("/api/runs/{run_id}/download")
def download_run(run_id: str):
    run = storage.get_run(run_id)
    if not run or not run["dataset_path"]:
        raise HTTPException(404, "dataset not built yet — call /api/runs/{run_id}/export first")
    path = Path(run["dataset_path"])
    if not path.exists():
        raise HTTPException(404, "dataset zip missing on disk")
    return FileResponse(path, filename=f"{run_id}_dataset.zip", media_type="application/zip")


class NoCacheStaticFiles(StaticFiles):
    """Plain StaticFiles caches aggressively enough that browsers will keep
    serving a stale index.html/app.js after an edit even on a hard reload —
    confusing during active local development, and easy to mistake for
    uvicorn --reload not picking up changes when it's actually the browser's
    disk cache. This is a local dev tool, not a CDN-fronted production app,
    so always-fresh static files are worth more than caching them."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-store"
        return response


app.mount("/", NoCacheStaticFiles(directory=str(BASE_DIR / "static"), html=True), name="static")
