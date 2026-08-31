"""NewsAPI.org connector (text/articles). Free dev tier: https://newsapi.org/register
(100 requests/day, articles capped at ~1 month old on the free tier)."""
from __future__ import annotations

import threading
import time
from typing import Optional

import requests

from config import NEWSAPI_KEY
from connectors.base import BaseConnector, Item, make_id, raise_for_connector_failure
from logging_setup import get_logger, log_event
from models import StructuredQuery

logger = get_logger(__name__)

API_URL = "https://newsapi.org/v2/everything"


class NewsApiConnector(BaseConnector):
    name = "newsapi"
    supported_types = {"text"}

    def is_configured(self) -> bool:
        return bool(NEWSAPI_KEY)

    def fetch(self, query: StructuredQuery, count: int, cancel_event: Optional[threading.Event] = None) -> list[Item]:
        items: list[Item] = []
        page_size = min(100, count)
        page = 1
        rate_limit_retries = 0
        params: dict = {
            "q": query.search_terms(),
            "pageSize": page_size,
            "sortBy": "relevancy",
            "apiKey": NEWSAPI_KEY,
        }
        if query.filters.language:
            params["language"] = query.filters.language
        dr = query.filters.date_range
        if dr:
            if dr.from_:
                params["from"] = dr.from_
            if dr.to:
                params["to"] = dr.to
        if query.filters.domain_allowlist:
            params["domains"] = ",".join(query.filters.domain_allowlist)

        while len(items) < count:
            if cancel_event is not None and cancel_event.is_set():
                break
            params["page"] = page
            try:
                resp = requests.get(API_URL, params=params, timeout=15)
            except requests.RequestException as exc:
                log_event(logger, "newsapi_request_failed", level=40, error=str(exc))
                if not items:
                    raise_for_connector_failure("NewsAPI", exc=exc)
                break
            if resp.status_code == 429:
                log_event(logger, "newsapi_rate_limited", level=30)
                rate_limit_retries += 1
                if not items and rate_limit_retries >= 5:
                    raise_for_connector_failure("NewsAPI", status_code=429)
                time.sleep(2)
                continue
            if resp.status_code != 200:
                log_event(logger, "newsapi_bad_status", level=40, status=resp.status_code, body=resp.text[:300])
                if not items:
                    raise_for_connector_failure("NewsAPI", status_code=resp.status_code)
                break
            data = resp.json()
            articles = data.get("articles", [])
            if not articles:
                break
            for art in articles:
                if len(items) >= count:
                    break
                source_url = art.get("url")
                if not source_url:
                    continue
                body = " ".join(filter(None, [art.get("description"), art.get("content")]))
                items.append(
                    Item(
                        id=make_id(source_url),
                        data_type="text",
                        source_name="newsapi",
                        source_url=source_url,
                        title=art.get("title"),
                        text=body or art.get("title"),
                        license="Content owned by original publisher; NewsAPI provides metadata/snippets only",
                        attribution=(art.get("source") or {}).get("name"),
                        author=art.get("author"),
                        published_at=art.get("publishedAt"),
                        query_text=query.search_terms(),
                        extra={"source_name_raw": (art.get("source") or {}).get("name")},
                    )
                )
            if page * page_size >= data.get("totalResults", 0):
                break
            page += 1
        log_event(logger, "newsapi_fetch_complete", requested=count, fetched=len(items))
        return items
