"""Pixabay API connector (images). Free tier: https://pixabay.com/api/docs/ (5000 req/hour)."""
from __future__ import annotations

import threading
import time
from typing import Optional

import requests

from config import PIXABAY_API_KEY
from connectors.base import BaseConnector, Item, make_id, raise_for_connector_failure
from logging_setup import get_logger, log_event
from models import StructuredQuery

logger = get_logger(__name__)

API_URL = "https://pixabay.com/api/"


class PixabayConnector(BaseConnector):
    name = "pixabay"
    supported_types = {"image"}

    def is_configured(self) -> bool:
        return bool(PIXABAY_API_KEY)

    def fetch(self, query: StructuredQuery, count: int, cancel_event: Optional[threading.Event] = None) -> list[Item]:
        items: list[Item] = []
        per_page = min(200, max(3, count))
        page = 1
        rate_limit_retries = 0
        params: dict = {
            "key": PIXABAY_API_KEY,
            "q": query.search_terms(),
            "per_page": per_page,
            "image_type": "photo",
        }
        if query.filters.orientation in ("landscape", "portrait"):
            params["orientation"] = query.filters.orientation
        if query.filters.resolution == "high":
            params["min_width"] = 1920

        while len(items) < count:
            if cancel_event is not None and cancel_event.is_set():
                break
            params["page"] = page
            try:
                resp = requests.get(API_URL, params=params, timeout=15)
            except requests.RequestException as exc:
                log_event(logger, "pixabay_request_failed", level=40, error=str(exc))
                if not items:
                    raise_for_connector_failure("Pixabay", exc=exc)
                break
            if resp.status_code == 429:
                log_event(logger, "pixabay_rate_limited", level=30)
                rate_limit_retries += 1
                if not items and rate_limit_retries >= 5:
                    raise_for_connector_failure("Pixabay", status_code=429)
                time.sleep(2)
                continue
            if resp.status_code != 200:
                log_event(logger, "pixabay_bad_status", level=40, status=resp.status_code, body=resp.text[:300])
                if not items:
                    raise_for_connector_failure("Pixabay", status_code=resp.status_code)
                break
            data = resp.json()
            hits = data.get("hits", [])
            if not hits:
                break
            for hit in hits:
                if len(items) >= count:
                    break
                source_url = hit.get("largeImageURL") or hit.get("webformatURL")
                if not source_url:
                    continue
                items.append(
                    Item(
                        id=make_id(source_url),
                        data_type="image",
                        source_name="pixabay",
                        source_url=source_url,
                        title=hit.get("tags"),
                        width=hit.get("imageWidth"),
                        height=hit.get("imageHeight"),
                        license="Pixabay License (free for commercial use, no attribution required)",
                        attribution=f"Image by {hit.get('user', 'unknown')} on Pixabay",
                        author=hit.get("user"),
                        query_text=query.search_terms(),
                        extra={"pixabay_id": hit.get("id")},
                    )
                )
            if page * per_page >= data.get("totalHits", 0):
                break
            page += 1
        log_event(logger, "pixabay_fetch_complete", requested=count, fetched=len(items))
        return items
