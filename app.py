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
from logging_setup import configure_logging, get_logger
from models import ExportOptions, StructuredQuery

configure_logging()
logger = get_logger("app")

app = FastAPI(title="DataFetcher", description="Fetch and package web data into ML-ready datasets.")


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


def _run_fetch_job(run_id: str, query: StructuredQuery, cancel_event: threading.Event) -> None:
    storage.update_run_status(run_id, "running")
    try:
        source_router.route(query, run_id, cancel_event=cancel_event)
        storage.update_run_status(run_id, "cancelled" if cancel_event.is_set() else "completed")
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


@app.get("/api/runs/{run_id}/results")
def run_results(run_id: str, offset: int = 0, limit: int = 60):
    run = storage.get_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    items = storage.list_items_for_run(run_id, offset, limit)
    total = storage.count_items_for_run(run_id)
    hint = None
    if total == 0 and run["status"] == "completed":
        structured = json.loads(run["structured_query"])
        hint = _zero_result_hint(structured.get("data_type", "text"))
    return {"items": items, "total": total, "run": run, "zero_result_hint": hint}


@app.get("/api/files/{item_id}")
def get_file(item_id: str):
    item = storage.get_item(item_id)
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


app.mount("/", StaticFiles(directory=str(BASE_DIR / "static"), html=True), name="static")
