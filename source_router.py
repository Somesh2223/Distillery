"""Decides where to fetch from: tries the configured API connector(s) for the
requested data_type first, and falls back to the generic scraper when no
connector is configured/enabled or the APIs returned fewer items than asked
for. Also owns the "materialize" step shared by every source: download/save
the raw content, run dedup, and persist metadata to SQLite.
"""
from __future__ import annotations

import concurrent.futures
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import requests
from requests.adapters import HTTPAdapter

import dedup
import storage
from config import FETCHED_DIR, MATERIALIZE_WORKERS
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

# A shared, connection-pooled session for image downloads — reused across the
# whole materialize thread pool so concurrent downloads to the same CDN (e.g.
# images.pexels.com) reuse TCP/TLS connections instead of each opening a new one.
_HTTP_SESSION = requests.Session()
_adapter = HTTPAdapter(pool_connections=MATERIALIZE_WORKERS, pool_maxsize=MATERIALIZE_WORKERS * 2)
_HTTP_SESSION.mount("http://", _adapter)
_HTTP_SESSION.mount("https://", _adapter)


def route(
    query: StructuredQuery,
    run_id: str,
    progress_cb: Optional[ProgressCallback] = None,
    cancel_event: Optional[threading.Event] = None,
    existing_count: int = 0,
    target_new: Optional[int] = None,
) -> list[Item]:
    """`existing_count` + `target_new` are for /topup calls: the run already
    has `existing_count` items, and we want `target_new` *new* ones on top.
    `query.count` then means "how large a candidate pool to ask connectors
    for" (bigger than target_new) rather than the stop threshold — asking a
    connector for exactly `target_new` items would just re-return the same
    top-ranked results as last time (deterministic ranking for the same
    query), which are already-known duplicates and dedup away to nothing.
    Requesting a larger pool lets dedup skip the ones already seen and
    surface enough new ones beyond that. For a normal (non-topup) fetch,
    target_new is None and this all reduces to the original behavior:
    target == query.count, existing_count == 0.
    """
    target = query.count if target_new is None else target_new
    materialized: list[Item] = []
    existing_phashes = [p for _id, p in storage.get_existing_phashes()]
    existing_text_sigs = [dedup.decode_signature(h) for _id, h in storage.get_existing_text_hashes()]

    def _cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    def _notify() -> None:
        total_so_far = existing_count + len(materialized)
        storage.set_run_fetched_count(run_id, total_so_far)
        if progress_cb:
            progress_cb(total_so_far, existing_count + target)

    state_lock = threading.Lock()

    def _consume(raw_items: list[Item]) -> None:
        """Materializes a batch concurrently — this is the dominant cost
        (network downloads), so running several at once is the main speed
        lever. `state_lock` keeps the shared dedup lists and `materialized`
        count consistent across worker threads."""
        if not raw_items:
            return

        def _worker(item: Item) -> None:
            if _cancelled():
                return
            with state_lock:
                if len(materialized) >= target:
                    return
            if _materialize_and_dedup(item, run_id, existing_phashes, existing_text_sigs, state_lock):
                with state_lock:
                    if len(materialized) < target:
                        materialized.append(item)
                        _notify()

        with concurrent.futures.ThreadPoolExecutor(max_workers=MATERIALIZE_WORKERS) as executor:
            futures = [executor.submit(_worker, item) for item in raw_items]
            for fut in concurrent.futures.as_completed(futures):
                fut.result()

    connectors = CONNECTORS_BY_TYPE.get(query.data_type, [])
    for connector in connectors:
        if len(materialized) >= target or _cancelled():
            break
        if not connector.is_configured():
            log_event(logger, "connector_skipped_not_configured", connector=connector.name)
            continue
        remaining = query.count - len(materialized)
        log_event(logger, "connector_selected", connector=connector.name, data_type=query.data_type, requested=remaining,
                   reason="configured connector matches data_type, trying before scraper fallback")
        try:
            raw_items = _fetch_with_keyword_fallback(connector.fetch, query, remaining, cancel_event, connector.name)
        except Exception as exc:
            log_event(logger, "connector_fetch_error", level=40, connector=connector.name, error=str(exc))
            continue
        _consume(raw_items)

    if _cancelled():
        log_event(logger, "route_cancelled", run_id=run_id, fetched=len(materialized))
        return materialized

    if len(materialized) < target:
        remaining = query.count - len(materialized)
        log_event(
            logger,
            "falling_back_to_scraper",
            level=30,
            requested=remaining,
            reason="no configured connector satisfied the full request for this data_type",
        )
        try:
            raw_items = _fetch_with_keyword_fallback(fallback_fetch, query, remaining, cancel_event, "scraper")
        except Exception as exc:
            log_event(logger, "scraper_fallback_error", level=40, error=str(exc))
            raw_items = []
        _consume(raw_items)

    if _cancelled():
        log_event(logger, "route_cancelled", run_id=run_id, fetched=len(materialized))
    else:
        log_event(logger, "route_complete", run_id=run_id, requested=target, fetched=len(materialized))
    return materialized


def _fetch_with_keyword_fallback(
    fetch_fn: Callable[..., list[Item]],
    query: StructuredQuery,
    count: int,
    cancel_event: Optional[threading.Event],
    source_name: str,
) -> list[Item]:
    """Calls `fetch_fn(query, count, cancel_event=...)` — a connector's fetch
    or the scraper's fallback_fetch — with the query as given (all keywords
    joined into one search string). If that comes up short and there's more
    than one keyword, retries with each keyword phrase individually.

    This matters because an LLM parser often returns several alternative
    phrasings in `keywords` (e.g. ["slippery surface", "wet floor", "ice"])
    rather than modifying words meant to combine into one phrase — joining
    all of them into a single search string can be too narrow/contradictory
    for a connector's search index to match anything.
    """
    items = fetch_fn(query, count, cancel_event=cancel_event)
    if len(items) >= count or len(query.keywords) <= 1:
        return items
    if cancel_event is not None and cancel_event.is_set():
        return items

    log_event(logger, "retrying_with_individual_keywords", source=source_name, keywords=query.keywords)
    seen_ids = {it.id for it in items}

    def _try_keyword(kw: str) -> list[Item]:
        if cancel_event is not None and cancel_event.is_set():
            return []
        narrowed = query.model_copy(update={"keywords": [kw]})
        try:
            # Each branch asks for the full `count` independently since they
            # run concurrently and don't know how much the others will
            # contribute — cheap here (these are metadata/search calls, not
            # the actual downloads) and we truncate when merging below.
            return fetch_fn(narrowed, count, cancel_event=cancel_event)
        except Exception as exc:
            log_event(logger, "keyword_retry_fetch_error", level=40, source=source_name, keyword=kw, error=str(exc))
            return []

    # These are independent search-API calls (not downloads), so run them
    # concurrently instead of waiting on each one-by-one.
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(query.keywords), 4)) as executor:
        per_keyword_results = list(executor.map(_try_keyword, query.keywords))

    # Merge round-robin (one item at a time from each keyword branch, in
    # rounds) rather than draining one branch before moving to the next.
    # Otherwise a generic phrase with abundant stock matches (e.g. "polished
    # marble floor") floods the result and crowds out rarer, more specific
    # ones (e.g. "icy sidewalk") that better match what was actually asked
    # for — even though every phrase came from the same keyword list.
    cursors = [0] * len(per_keyword_results)
    progress = True
    while len(items) < count and progress:
        progress = False
        for branch_idx, branch_items in enumerate(per_keyword_results):
            if len(items) >= count:
                break
            idx = cursors[branch_idx]
            while idx < len(branch_items):
                it = branch_items[idx]
                idx += 1
                if it.id not in seen_ids:
                    seen_ids.add(it.id)
                    items.append(it)
                    progress = True
                    break
            cursors[branch_idx] = idx
    return items


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


def _materialize_and_dedup(
    item: Item, run_id: str, existing_phashes: list[str], existing_text_sigs: list[list[int]], lock: threading.Lock
) -> bool:
    try:
        if item.data_type == "image":
            return _materialize_image(item, run_id, existing_phashes, lock)
        return _materialize_textlike(item, run_id, existing_text_sigs, lock)
    except Exception as exc:
        log_event(logger, "materialize_failed", level=40, item_id=item.id, error=str(exc))
        return False


def _materialize_image(item: Item, run_id: str, existing_phashes: list[str], lock: threading.Lock) -> bool:
    try:
        resp = _HTTP_SESSION.get(item.source_url, timeout=20, stream=True, headers={"User-Agent": "DataFetcher/0.1"})
        resp.raise_for_status()
    except requests.RequestException as exc:
        log_event(logger, "image_download_failed", level=30, url=item.source_url, error=str(exc))
        return False

    content_type = resp.headers.get("Content-Type", "")
    ext = _guess_extension(item.source_url, content_type)
    rel_dir = Path(run_id) / "images"
    (FETCHED_DIR / rel_dir).mkdir(parents=True, exist_ok=True)  # exist_ok=True is safe under concurrent calls
    rel_path = rel_dir / f"{item.id}{ext}"
    abs_path = FETCHED_DIR / rel_path
    with open(abs_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=65536):
            f.write(chunk)

    phash = dedup.phash_for_image(str(abs_path))
    if phash is None:
        abs_path.unlink(missing_ok=True)
        return False

    # The check-against-existing and reserve-this-hash step must be atomic
    # w.r.t. other concurrent downloads, or two near-duplicate photos
    # downloading at the same time could both pass the check.
    with lock:
        is_dup = dedup.is_duplicate_image(phash, existing_phashes)
        if not is_dup:
            existing_phashes.append(phash)
    if is_dup:
        log_event(logger, "duplicate_image_skipped", item_id=item.id, source_url=item.source_url)
        abs_path.unlink(missing_ok=True)
        return False

    item.local_path = str(rel_path).replace("\\", "/")
    item.phash = phash
    item.fetched_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if not storage.insert_item(run_id, item):
        with lock:
            if phash in existing_phashes:
                existing_phashes.remove(phash)
        abs_path.unlink(missing_ok=True)
        return False
    return True


def _materialize_textlike(item: Item, run_id: str, existing_text_sigs: list[list[int]], lock: threading.Lock) -> bool:
    text_for_hash = item.text or item.title or ""
    if not text_for_hash.strip():
        return False
    sig = dedup.text_signature(text_for_hash)

    with lock:
        is_dup = dedup.is_duplicate_text(sig, existing_text_sigs)
        if not is_dup:
            existing_text_sigs.append(sig)
    if is_dup:
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
        with lock:
            if sig in existing_text_sigs:
                existing_text_sigs.remove(sig)
        return False
    return True
