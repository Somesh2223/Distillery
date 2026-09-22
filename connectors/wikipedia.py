"""Wikipedia connectors — free, no API key required.

- WikipediaConnector: text articles/summaries (data_type="text")
- WikipediaTableConnector: tables/listings/rankings (data_type="structured"),
  e.g. "top 100 companies by market cap" -> rows of the matching Wikipedia table.

All content is CC BY-SA 4.0 (https://en.wikipedia.org/wiki/Wikipedia:Copyrights).
"""
from __future__ import annotations

import concurrent.futures
import json
import threading
from typing import Optional

import requests

from connectors.base import BaseConnector, Item, make_id, raise_for_connector_failure
from logging_setup import get_logger, log_event
from models import StructuredQuery

logger = get_logger(__name__)

API_URL = "https://en.wikipedia.org/w/api.php"
# Wikimedia's API etiquette policy throttles or blocks generic user agents;
# it asks for a tool name, version and a contact URL. A bare "Distillery/0.1"
# was drawing sustained 429s.
_HEADERS = {"User-Agent": "Distillery/0.1 (https://github.com/Somesh2223/Distillery)"}
LICENSE = "CC BY-SA 4.0 (Wikipedia)"


def _search_titles(term: str, limit: int) -> list[str]:
    """Always the first network call for both connectors below (before either
    has any items), so a failure here means the whole fetch will yield
    nothing — worth raising a specific reason rather than returning []."""
    params = {
        "action": "query",
        "list": "search",
        "srsearch": term,
        "srlimit": min(limit, 50),
        "format": "json",
    }
    try:
        resp = requests.get(API_URL, params=params, timeout=15, headers=_HEADERS)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log_event(logger, "wikipedia_search_failed", level=40, error=str(exc))
        raise_for_connector_failure("Wikipedia", exc=exc)
        return []
    return [r["title"] for r in resp.json().get("query", {}).get("search", [])]


_EXTRACTS_PER_REQUEST = 20  # MediaWiki's exlimit ceiling for anonymous callers


def _get_extracts(
    titles: list[str], cancel_event: Optional[threading.Event] = None
) -> tuple[dict[str, str], dict[str, str]]:
    """Batch-fetch intro extracts, 20 titles per request.

    Returns (intro by canonical title, requested title -> canonical title).

    `exlimit` only accepts more than one title when `exintro` is set, so this
    can't return whole articles — its job is to establish cheaply which search
    hits actually have content, in one or two requests instead of one per
    title. The kept titles are then upgraded to full text by _get_full_extract,
    and these intros stand in for any upgrade that fails.
    """
    extracts: dict[str, str] = {}
    canonical: dict[str, str] = {}
    for start in range(0, len(titles), _EXTRACTS_PER_REQUEST):
        if cancel_event is not None and cancel_event.is_set():
            break
        chunk = titles[start:start + _EXTRACTS_PER_REQUEST]
        params = {
            "action": "query",
            "prop": "extracts",
            "explaintext": 1,
            "exintro": 1,
            "exlimit": _EXTRACTS_PER_REQUEST,
            "redirects": 1,
            "titles": "|".join(chunk),
            "format": "json",
        }
        try:
            resp = requests.get(API_URL, params=params, timeout=20, headers=_HEADERS)
            resp.raise_for_status()
        except requests.RequestException as exc:
            log_event(logger, "wikipedia_extracts_failed", level=40, titles=len(chunk), error=str(exc))
            continue
        data = resp.json().get("query", {})
        # Wikipedia rewrites titles it normalizes or redirects, so the page
        # that comes back can be filed under a different name than we asked
        # for — without this mapping those results look like misses.
        for mapping_key in ("normalized", "redirects"):
            for entry in data.get(mapping_key, []) or []:
                canonical[entry.get("from", "")] = entry.get("to", "")
        for page in (data.get("pages") or {}).values():
            title, extract = page.get("title"), page.get("extract")
            if title and extract:
                extracts[title] = extract
    return extracts, canonical


_EXTRACT_WORKERS = 6


def _get_full_extract(title: str) -> tuple[str, Optional[str]]:
    """Whole-article plaintext for one title. Only called for the articles
    actually being kept, so this costs `count` requests rather than one per
    search hit, and they run concurrently."""
    params = {
        "action": "query",
        "prop": "extracts",
        "explaintext": 1,
        "redirects": 1,
        "titles": title,
        "format": "json",
    }
    try:
        resp = requests.get(API_URL, params=params, timeout=20, headers=_HEADERS)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log_event(logger, "wikipedia_extract_failed", level=30, title=title, error=str(exc))
        return title, None
    for page in (resp.json().get("query", {}).get("pages") or {}).values():
        if page.get("extract"):
            return title, page["extract"]
    return title, None


class WikipediaConnector(BaseConnector):
    name = "wikipedia"
    supported_types = {"text"}

    def is_configured(self) -> bool:
        return True

    def fetch(self, query: StructuredQuery, count: int, cancel_event: Optional[threading.Event] = None) -> list[Item]:
        titles = _search_titles(query.search_terms(), count * 2)
        # One batched call says which of these titles actually have content,
        # then only the ones being kept are upgraded to full article text.
        intros, canonical = _get_extracts(titles, cancel_event)
        wanted: list[str] = []
        for title in titles:
            # normalized -> redirect can chain, so follow it rather than
            # resolving a single hop (bounded in case of a redirect loop).
            for _ in range(4):
                if title in intros or title not in canonical:
                    break
                title = canonical[title]
            if title in intros and title not in wanted:
                wanted.append(title)
            if len(wanted) >= count:
                break

        full: dict[str, str] = {}
        if wanted and not (cancel_event is not None and cancel_event.is_set()):
            workers = max(1, min(_EXTRACT_WORKERS, len(wanted)))
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                for title, text in executor.map(_get_full_extract, wanted):
                    if text:
                        full[title] = text

        items: list[Item] = []
        for title in wanted:
            if len(items) >= count or (cancel_event is not None and cancel_event.is_set()):
                break
            # Fall back to the intro when the full-text upgrade failed, so a
            # flaky request costs detail rather than the whole article.
            summary = full.get(title) or intros.get(title)
            if not summary:
                continue
            page_url = f"https://en.wikipedia.org/wiki/{title.replace(' ', '_')}"
            items.append(
                Item(
                    id=make_id(page_url),
                    data_type="text",
                    source_name="wikipedia",
                    source_url=page_url,
                    title=title,
                    text=summary,
                    license=LICENSE,
                    attribution=f"Wikipedia contributors, '{title}'",
                    query_text=query.search_terms(),
                )
            )
        log_event(logger, "wikipedia_fetch_complete", requested=count, fetched=len(items))
        return items


class WikipediaTableConnector(BaseConnector):
    name = "wikipedia_tables"
    supported_types = {"structured"}

    def is_configured(self) -> bool:
        return True

    def fetch(self, query: StructuredQuery, count: int, cancel_event: Optional[threading.Event] = None) -> list[Item]:
        titles = _search_titles(query.search_terms(), 5)
        for title in titles:
            if cancel_event is not None and cancel_event.is_set():
                break
            items = self._extract_table_rows(title, count, query.search_terms())
            if items:
                log_event(logger, "wikipedia_table_fetch_complete", title=title, requested=count, fetched=len(items))
                return items
        log_event(logger, "wikipedia_table_no_match", level=30, terms=query.search_terms())
        return []

    def _extract_table_rows(self, title: str, count: int, query_text: str) -> list[Item]:
        try:
            import pandas as pd
        except ImportError:
            log_event(logger, "pandas_not_installed", level=40)
            return []

        page_url = f"https://en.wikipedia.org/wiki/{title.replace(' ', '_')}"
        try:
            resp = requests.get(page_url, timeout=20, headers=_HEADERS)
            resp.raise_for_status()
        except requests.RequestException as exc:
            log_event(logger, "wikipedia_page_fetch_failed", level=40, title=title, error=str(exc))
            return []

        try:
            import io

            tables = pd.read_html(io.StringIO(resp.text))
        except ValueError:
            return []
        if not tables:
            return []

        best = max(tables, key=lambda df: df.shape[0] * df.shape[1])
        best = best.head(count)
        best.columns = [str(c) for c in best.columns]

        items: list[Item] = []
        for i, row in best.iterrows():
            row_dict = {str(k): (None if pd.isna(v) else str(v)) for k, v in row.items()}
            row_url = f"{page_url}#row-{i}"
            items.append(
                Item(
                    id=make_id(row_url),
                    data_type="structured",
                    source_name="wikipedia_tables",
                    source_url=page_url,
                    title=f"{title} (row {i})",
                    text=json.dumps(row_dict, ensure_ascii=False),
                    license=LICENSE,
                    attribution=f"Wikipedia contributors, '{title}'",
                    query_text=query_text,
                    extra={"columns": list(best.columns), "row": row_dict, "row_index": int(i)},
                )
            )
        return items
