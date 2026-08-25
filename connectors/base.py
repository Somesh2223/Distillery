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

from models import StructuredQuery


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
