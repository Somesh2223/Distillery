"""Shared Item model and connector interface.

Every connector in this package (unsplash, pexels, pixabay, newsapi, wikipedia,
reddit, hackernews, ...) implements `fetch(query, count) -> list[Item]` so that
source_router.py can treat them interchangeably.
"""
from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field
from typing import Any, Optional

import requests

from models import StructuredQuery


class ConnectorUnavailableError(Exception):
    """Raised when a connector couldn't reach its API at all — no internet,
    an invalid/expired API key, a persistent rate limit, or the API's own
    servers being down — as opposed to reaching it successfully and finding
    zero matches, which is not an error.

    Connectors should raise this only when they have gathered NO items yet
    (the very first request failed); if they already have some items from
    earlier pages, they should keep the existing behavior of logging and
    returning the partial results instead, so one flaky request doesn't
    discard otherwise-good partial data.
    """

    def __init__(self, category: str, message: str):
        # category: "no_internet" | "timeout" | "auth" | "rate_limited" | "server_error" | "unknown"
        self.category = category
        super().__init__(message)


# Priority order for picking the single most useful message when several
# sources failed for different reasons — a dead network subsumes everything
# else, an expired key is more actionable than a generic timeout, etc.
CATEGORY_PRIORITY = {"no_internet": 0, "auth": 1, "rate_limited": 2, "server_error": 3, "timeout": 4, "unknown": 5}


def raise_for_connector_failure(
    service_name: str,
    *,
    exc: Optional[Exception] = None,
    status_code: Optional[int] = None,
) -> None:
    """Classify a request failure and raise ConnectorUnavailableError with a
    specific, actionable message. Call this from a connector's first-page
    request only (see ConnectorUnavailableError docstring)."""
    if exc is not None:
        if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.SSLError)):
            raise ConnectorUnavailableError(
                "no_internet", f"Could not reach {service_name} — check your internet connection."
            ) from exc
        if isinstance(exc, requests.exceptions.Timeout):
            raise ConnectorUnavailableError(
                "timeout", f"{service_name} did not respond in time — your connection may be slow or down."
            ) from exc
        if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
            # e.g. raised by resp.raise_for_status() — recurse into the
            # status-code classification below instead of falling to
            # "unknown". If the status code isn't one of the recognized
            # ones, this returns normally and we fall through to "unknown".
            raise_for_connector_failure(service_name, status_code=exc.response.status_code)
        raise ConnectorUnavailableError("unknown", f"{service_name} request failed: {exc}") from exc
    if status_code is not None:
        if status_code in (401, 403):
            raise ConnectorUnavailableError(
                "auth",
                f"{service_name} rejected the request (HTTP {status_code}) — check that its API key in .env is "
                "correct, active, and not expired.",
            )
        if status_code == 429:
            raise ConnectorUnavailableError(
                "rate_limited", f"{service_name} rate limit exceeded (HTTP 429) — wait a bit and try again."
            )
        if status_code >= 500:
            raise ConnectorUnavailableError(
                "server_error", f"{service_name} is having server issues (HTTP {status_code}) — try again later."
            )


def make_id(source_url: str) -> str:
    """Stable dedup key derived from the canonical source URL."""
    return hashlib.sha256(source_url.strip().lower().encode("utf-8")).hexdigest()[:24]


@dataclass
class Item:
    id: str
    data_type: str  # image | text | structured
    source_name: str  # e.g. "unsplash", "newsapi", "scraper:example.com"
    source_url: str
    title: Optional[str] = None
    text: Optional[str] = None  # article body / snippet / structured summary
    local_path: Optional[str] = None  # path relative to DATA_DIR
    width: Optional[int] = None
    height: Optional[int] = None
    license: Optional[str] = None
    attribution: Optional[str] = None
    author: Optional[str] = None
    published_at: Optional[str] = None
    query_text: str = ""
    fetched_at: str = ""
    phash: Optional[str] = None
    text_hash: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)


class BaseConnector:
    """Common interface every API connector implements."""

    name: str = "base"
    supported_types: set[str] = set()

    def is_configured(self) -> bool:
        """Whether required API keys/env vars are present."""
        raise NotImplementedError

    def fetch(self, query: StructuredQuery, count: int, cancel_event: Optional[threading.Event] = None) -> list[Item]:
        """Fetch up to `count` items matching the structured query.

        Implementations should never raise on ordinary API errors (rate
        limit, no results, etc) except to let the caller log and fall back;
        raising is reserved for programmer errors.

        `cancel_event`, if given, should be checked between pages/requests
        (not just at the top) so a user-triggered stop takes effect within
        roughly one page-fetch, not only after `count` is fully satisfied.
        """
        raise NotImplementedError
