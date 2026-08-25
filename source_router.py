"""Decides where to fetch from: tries the configured API connector(s) for the
requested data_type first, and falls back to the generic scraper when no
connector is configured/enabled or the APIs returned fewer items than asked
for. Also owns the "materialize" step shared by every source: download/save
the raw content, run dedup, and persist metadata to SQLite.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Optional

import requests

import dedup
import storage
from config import FETCHED_DIR
from connectors.base import Item
from connectors.hackernews import HackerNewsConnector
from connectors.newsapi import NewsApiConnector
from connectors.pexels import PexelsConnector
from connectors.pixabay import PixabayConnector
from connectors.reddit import RedditConnector
from connectors.unsplash import UnsplashConnector
from connectors.wikipedia import WikipediaConnector, WikipediaTableConnector
from logging_setup import get_logger, log_event
from models import StructuredQuery
from scraper import fallback_fetch

logger = get_logger(__name__)

CONNECTORS_BY_TYPE: dict[str, list] = {
    "image": [UnsplashConnector(), PexelsConnector(), PixabayConnector()],
    "text": [NewsApiConnector(), WikipediaConnector(), HackerNewsConnector(), RedditConnector()],
    "structured": [WikipediaTableConnector()],
}

ProgressCallback = Callable[[int, int], None]  # (fetched_count, requested_count)


def route(query: StructuredQuery, run_id: str, progress_cb: Optional[ProgressCallback] = None) -> list[Item]:
    materialized: list[Item] = []
    existing_phashes = [p for _id, p in storage.get_existing_phashes()]
    existing_text_sigs = [dedup.decode_signature(h) for _id, h in storage.get_existing_text_hashes()]

    def _notify() -> None:
        storage.set_run_fetched_count(run_id, len(materialized))
        if progress_cb:
            progress_cb(len(materialized), query.count)

    connectors = CONNECTORS_BY_TYPE.get(query.data_type, [])
    for connector in connectors:
        if len(materialized) >= query.count:
            break
        if not connector.is_configured():
            log_event(logger, "connector_skipped_not_configured", connector=connector.name)
            continue
        remaining = query.count - len(materialized)
        log_event(logger, "connector_selected", connector=connector.name, data_type=query.data_type, requested=remaining,
                   reason="configured connector matches data_type, trying before scraper fallback")
        try:
            raw_items = connector.fetch(query, remaining)
        except Exception as exc:
            log_event(logger, "connector_fetch_error", level=40, connector=connector.name, error=str(exc))
            continue
        for item in raw_items:
            if len(materialized) >= query.count:
                break
            if _materialize_and_dedup(item, run_id, existing_phashes, existing_text_sigs):
                materialized.append(item)
                _notify()

    if len(materialized) < query.count:
        remaining = query.count - len(materialized)
        log_event(
            logger,
            "falling_back_to_scraper",
            level=30,
            requested=remaining,
            reason="no configured connector satisfied the full request for this data_type",
        )
        try:
            raw_items = fallback_fetch(query, remaining)
        except Exception as exc:
            log_event(logger, "scraper_fallback_error", level=40, error=str(exc))
            raw_items = []
        for item in raw_items:
            if len(materialized) >= query.count:
                break
            if _materialize_and_dedup(item, run_id, existing_phashes, existing_text_sigs):
                materialized.append(item)
                _notify()

    log_event(logger, "route_complete", run_id=run_id, requested=query.count, fetched=len(materialized))
    return materialized


def _guess_extension(url: str, content_type: str) -> str:
    for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        if url.lower().split("?")[0].endswith(ext):
            return ext
    if "png" in content_type:
        return ".png"
    if "webp" in content_type:
        return ".webp"
    if "gif" in content_type:
        return ".gif"
    return ".jpg"


def _materialize_and_dedup(item: Item, run_id: str, existing_phashes: list[str], existing_text_sigs: list[list[int]]) -> bool:
    try:
        if item.data_type == "image":
            return _materialize_image(item, run_id, existing_phashes)
        return _materialize_textlike(item, run_id, existing_text_sigs)
    except Exception as exc:
        log_event(logger, "materialize_failed", level=40, item_id=item.id, error=str(exc))
        return False


def _materialize_image(item: Item, run_id: str, existing_phashes: list[str]) -> bool:
    try:
        resp = requests.get(item.source_url, timeout=20, stream=True, headers={"User-Agent": "DataFetcher/0.1"})
        resp.raise_for_status()
    except requests.RequestException as exc:
        log_event(logger, "image_download_failed", level=30, url=item.source_url, error=str(exc))
        return False

    content_type = resp.headers.get("Content-Type", "")
    ext = _guess_extension(item.source_url, content_type)
    rel_dir = Path(run_id) / "images"
    (FETCHED_DIR / rel_dir).mkdir(parents=True, exist_ok=True)
    rel_path = rel_dir / f"{item.id}{ext}"
    abs_path = FETCHED_DIR / rel_path
    with open(abs_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=65536):
            f.write(chunk)

    phash = dedup.phash_for_image(str(abs_path))
    if phash is None:
        abs_path.unlink(missing_ok=True)
        return False
    if dedup.is_duplicate_image(phash, existing_phashes):
        log_event(logger, "duplicate_image_skipped", item_id=item.id, source_url=item.source_url)
        abs_path.unlink(missing_ok=True)
        return False

    item.local_path = str(rel_path).replace("\\", "/")
    item.phash = phash
    item.fetched_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if not storage.insert_item(run_id, item):
        abs_path.unlink(missing_ok=True)
        return False
    existing_phashes.append(phash)
    return True


def _materialize_textlike(item: Item, run_id: str, existing_text_sigs: list[list[int]]) -> bool:
    text_for_hash = item.text or item.title or ""
    if not text_for_hash.strip():
        return False
    sig = dedup.text_signature(text_for_hash)
    if dedup.is_duplicate_text(sig, existing_text_sigs):
        log_event(logger, "duplicate_text_skipped", item_id=item.id, source_url=item.source_url)
        return False

    subdir = "structured" if item.data_type == "structured" else "text"
    rel_dir = Path(run_id) / subdir
    (FETCHED_DIR / rel_dir).mkdir(parents=True, exist_ok=True)
    rel_path = rel_dir / f"{item.id}.txt"
    abs_path = FETCHED_DIR / rel_path
    abs_path.write_text(text_for_hash, encoding="utf-8")

    item.local_path = str(rel_path).replace("\\", "/")
    item.text_hash = dedup.encode_signature(sig)
    item.fetched_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if not storage.insert_item(run_id, item):
        return False
    existing_text_sigs.append(sig)
    return True
