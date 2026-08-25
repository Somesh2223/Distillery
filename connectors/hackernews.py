"""Hacker News connector (text) via the Algolia HN Search API — free, no API key.
https://hn.algolia.com/api"""
from __future__ import annotations

import requests

from connectors.base import BaseConnector, Item, make_id
from logging_setup import get_logger, log_event
from models import StructuredQuery

logger = get_logger(__name__)

API_URL = "https://hn.algolia.com/api/v1/search"


class HackerNewsConnector(BaseConnector):
    name = "hackernews"
    supported_types = {"text"}

    def is_configured(self) -> bool:
        return True  # no key required

    def fetch(self, query: StructuredQuery, count: int) -> list[Item]:
        items: list[Item] = []
        page = 0
        hits_per_page = min(100, count)
        while len(items) < count:
            params = {
                "query": query.search_terms(),
                "tags": "story",
                "hitsPerPage": hits_per_page,
                "page": page,
            }
            dr = query.filters.date_range
            if dr and (dr.from_ or dr.to):
                filters = []
                if dr.from_:
                    filters.append(f"created_at_i>{_to_epoch(dr.from_)}")
                if dr.to:
                    filters.append(f"created_at_i<{_to_epoch(dr.to)}")
                params["numericFilters"] = ",".join(filters)
            try:
                resp = requests.get(API_URL, params=params, timeout=15)
            except requests.RequestException as exc:
                log_event(logger, "hackernews_request_failed", level=40, error=str(exc))
                break
            if resp.status_code != 200:
                log_event(logger, "hackernews_bad_status", level=40, status=resp.status_code)
                break
            data = resp.json()
            hits = data.get("hits", [])
            if not hits:
                break
            for hit in hits:
                if len(items) >= count:
                    break
                source_url = hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
                title = hit.get("title") or ""
                text = hit.get("story_text") or title
                items.append(
                    Item(
                        id=make_id(source_url),
                        data_type="text",
                        source_name="hackernews",
                        source_url=source_url,
                        title=title,
                        text=text,
                        license="Public HN metadata (CC-BY-SA-like community content); linked article may carry its own license",
                        attribution="Hacker News / hn.algolia.com",
                        author=hit.get("author"),
                        published_at=hit.get("created_at"),
                        query_text=query.search_terms(),
                        extra={"points": hit.get("points"), "num_comments": hit.get("num_comments")},
                    )
                )
            if data.get("nbPages", 1) <= page + 1:
                break
            page += 1
        log_event(logger, "hackernews_fetch_complete", requested=count, fetched=len(items))
        return items


def _to_epoch(iso_date: str) -> int:
    import datetime

    try:
        return int(datetime.datetime.fromisoformat(iso_date).timestamp())
    except ValueError:
        return 0
