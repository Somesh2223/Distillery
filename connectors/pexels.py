"""Pexels API connector (images). Free tier: https://www.pexels.com/api/ (200 req/hour)."""
from __future__ import annotations

import threading
import time
from typing import Optional

import requests

from config import PEXELS_API_KEY
from connectors.base import BaseConnector, Item, make_id, raise_for_connector_failure
from logging_setup import get_logger, log_event
from models import StructuredQuery

logger = get_logger(__name__)

API_URL = "https://api.pexels.com/v1/search"


class PexelsConnector(BaseConnector):
    name = "pexels"
    supported_types = {"image"}

    def is_configured(self) -> bool:
        return bool(PEXELS_API_KEY)

    def fetch(self, query: StructuredQuery, count: int, cancel_event: Optional[threading.Event] = None) -> list[Item]:
        items: list[Item] = []
        per_page = min(80, count)
        page = 1
        rate_limit_retries = 0
        headers = {"Authorization": PEXELS_API_KEY}
        params: dict = {"query": query.search_terms(), "per_page": per_page}
        if query.filters.orientation:
            params["orientation"] = query.filters.orientation

        while len(items) < count:
            if cancel_event is not None and cancel_event.is_set():
                break
            params["page"] = page
            try:
                resp = requests.get(API_URL, headers=headers, params=params, timeout=15)
            except requests.RequestException as exc:
                log_event(logger, "pexels_request_failed", level=40, error=str(exc))
                if not items:
                    raise_for_connector_failure("Pexels", exc=exc)
                break
            if resp.status_code == 429:
                log_event(logger, "pexels_rate_limited", level=30)
                rate_limit_retries += 1
                if not items and rate_limit_retries >= 5:
                    raise_for_connector_failure("Pexels", status_code=429)
                time.sleep(2)
                continue
            if resp.status_code != 200:
                log_event(logger, "pexels_bad_status", level=40, status=resp.status_code, body=resp.text[:300])
                if not items:
                    raise_for_connector_failure("Pexels", status_code=resp.status_code)
                break
            data = resp.json()
            photos = data.get("photos", [])
            if not photos:
                break
            for photo in photos:
                if len(items) >= count:
                    break
                src = photo.get("src", {})
                source_url = src.get("large2x") or src.get("original") or src.get("large")
                if not source_url:
                    continue
                items.append(
                    Item(
                        id=make_id(source_url),
                        data_type="image",
                        source_name="pexels",
                        source_url=source_url,
                        title=photo.get("alt"),
                        width=photo.get("width"),
                        height=photo.get("height"),
                        license="Pexels License (free to use, no attribution required)",
                        attribution=f"Photo by {photo.get('photographer', 'unknown')} on Pexels",
                        author=photo.get("photographer"),
                        query_text=query.search_terms(),
                        # page_url: source_url is the raw CDN image file we
                        # download, not something a human wants to open —
                        # the UI's "view source" link uses this instead.
                        extra={
                            "pexels_id": photo.get("id"),
                            "photographer_url": photo.get("photographer_url"),
                            "page_url": photo.get("url"),
                        },
                    )
                )
            if not data.get("next_page"):
                break
            page += 1
        log_event(logger, "pexels_fetch_complete", requested=count, fetched=len(items))
        return items
