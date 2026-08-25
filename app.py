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
from config import BASE_DIR, FETCHED_DIR
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


@app.post("/api/parse")
def parse_condition(req: ParseRequest):
    if not req.condition.strip():
        raise HTTPException(400, "condition must not be empty")
    query = query_parser.parse_condition(req.condition)
    return query.model_dump(by_alias=True)


def _run_fetch_job(run_id: str, query: StructuredQuery) -> None:
    storage.update_run_status(run_id, "running")
    try:
        source_router.route(query, run_id)
        storage.update_run_status(run_id, "completed")
    except Exception as exc:
        logger.exception("fetch job failed for run %s", run_id)
        storage.update_run_status(run_id, "failed", error=str(exc))


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
    thread = threading.Thread(target=_run_fetch_job, args=(run_id, req.structured_query), daemon=True)
    thread.start()
    return {"run_id": run_id}


@app.get("/api/runs/{run_id}/status")
def run_status(run_id: str):
    run = storage.get_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    return run


@app.get("/api/runs/{run_id}/results")
def run_results(run_id: str, offset: int = 0, limit: int = 60):
    run = storage.get_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    items = storage.list_items_for_run(run_id, offset, limit)
    total = storage.count_items_for_run(run_id)
    return {"items": items, "total": total, "run": run}


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

    try:
        zip_path = dataset_export.export_dataset(
            run_id, structured.get("label", "dataset"), structured.get("data_type", "text"), options
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
