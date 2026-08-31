"""Unsplash API connector (images). Free tier: https://unsplash.com/developers
(Demo apps get 50 requests/hour)."""
from __future__ import annotations

import threading
import time
from typing import Optional

import requests

from config import UNSPLASH_ACCESS_KEY
from connectors.base import BaseConnector, Item, make_id, raise_for_connector_failure
from logging_setup import get_logger, log_event
from models import StructuredQuery

logger = get_logger(__name__)

API_URL = "https://api.unsplash.com/search/photos"


class UnsplashConnector(BaseConnector):
    name = "unsplash"
    supported_types = {"image"}

    def is_configured(self) -> bool:
        return bool(UNSPLASH_ACCESS_KEY)

    def fetch(self, query: StructuredQuery, count: int, cancel_event: Optional[threading.Event] = None) -> list[Item]:
        items: list[Item] = []
        per_page = min(30, count)
        page = 1
        rate_limit_retries = 0
        headers = {"Authorization": f"Client-ID {UNSPLASH_ACCESS_KEY}"}
        params: dict = {
            "query": query.search_terms(),
            "per_page": per_page,
        }
        if query.filters.orientation:
            params["orientation"] = query.filters.orientation

        while len(items) < count:
            if cancel_event is not None and cancel_event.is_set():
                break
            params["page"] = page
            try:
                resp = requests.get(API_URL, headers=headers, params=params, timeout=15)
            except requests.RequestException as exc:
                log_event(logger, "unsplash_request_failed", level=40, error=str(exc))
                if not items:
                    raise_for_connector_failure("Unsplash", exc=exc)
                break
            if resp.status_code == 429:
                log_event(logger, "unsplash_rate_limited", level=30)
                rate_limit_retries += 1
                if not items and rate_limit_retries >= 5:
                    raise_for_connector_failure("Unsplash", status_code=429)
                time.sleep(2)
                continue
            if resp.status_code != 200:
                log_event(logger, "unsplash_bad_status", level=40, status=resp.status_code, body=resp.text[:300])
                if not items:
                    raise_for_connector_failure("Unsplash", status_code=resp.status_code)
                break
            data = resp.json()
            results = data.get("results", [])
            if not results:
                break
            for photo in results:
                if len(items) >= count:
                    break
                urls = photo.get("urls", {})
                source_url = urls.get("regular") or urls.get("full") or urls.get("raw")
                if not source_url:
                    continue
                user = photo.get("user", {}) or {}
                items.append(
                    Item(
                        id=make_id(source_url),
                        data_type="image",
                        source_name="unsplash",
                        source_url=source_url,
                        title=photo.get("description") or photo.get("alt_description"),
                        width=photo.get("width"),
                        height=photo.get("height"),
                        license="Unsplash License (free to use, attribution appreciated)",
                        attribution=f"Photo by {user.get('name', 'unknown')} on Unsplash",
                        author=user.get("name"),
                        published_at=photo.get("created_at"),
                        query_text=query.search_terms(),
                        extra={"unsplash_id": photo.get("id"), "links": photo.get("links", {})},
                    )
                )
            if page * per_page >= data.get("total", 0):
                break
            page += 1
        log_event(logger, "unsplash_fetch_complete", requested=count, fetched=len(items))
        return items
