"""Wikipedia connectors — free, no API key required.

- WikipediaConnector: text articles/summaries (data_type="text")
- WikipediaTableConnector: tables/listings/rankings (data_type="structured"),
  e.g. "top 100 companies by market cap" -> rows of the matching Wikipedia table.

All content is CC BY-SA 4.0 (https://en.wikipedia.org/wiki/Wikipedia:Copyrights).
"""
from __future__ import annotations

import json
import time

import requests

from connectors.base import BaseConnector, Item, make_id
from logging_setup import get_logger, log_event
from models import StructuredQuery

logger = get_logger(__name__)

API_URL = "https://en.wikipedia.org/w/api.php"
LICENSE = "CC BY-SA 4.0 (Wikipedia)"


def _search_titles(term: str, limit: int) -> list[str]:
    params = {
        "action": "query",
        "list": "search",
        "srsearch": term,
        "srlimit": min(limit, 50),
        "format": "json",
    }
    try:
        resp = requests.get(API_URL, params=params, timeout=15, headers={"User-Agent": "DataFetcher/0.1"})
        resp.raise_for_status()
    except requests.RequestException as exc:
        log_event(logger, "wikipedia_search_failed", level=40, error=str(exc))
        return []
    return [r["title"] for r in resp.json().get("query", {}).get("search", [])]


class WikipediaConnector(BaseConnector):
    name = "wikipedia"
    supported_types = {"text"}

    def is_configured(self) -> bool:
        return True

    def fetch(self, query: StructuredQuery, count: int) -> list[Item]:
        titles = _search_titles(query.search_terms(), count * 2)
        items: list[Item] = []
        for title in titles:
            if len(items) >= count:
                break
            summary = self._get_extract(title)
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
            time.sleep(0.1)  # be polite to the shared MediaWiki API
        log_event(logger, "wikipedia_fetch_complete", requested=count, fetched=len(items))
        return items

    def _get_extract(self, title: str) -> str | None:
        params = {
            "action": "query",
            "prop": "extracts",
            "explaintext": 1,
            "titles": title,
            "format": "json",
        }
        try:
            resp = requests.get(API_URL, params=params, timeout=15, headers={"User-Agent": "DataFetcher/0.1"})
            resp.raise_for_status()
        except requests.RequestException as exc:
            log_event(logger, "wikipedia_extract_failed", level=40, title=title, error=str(exc))
            return None
        pages = resp.json().get("query", {}).get("pages", {})
        for page in pages.values():
            extract = page.get("extract")
            if extract:
                return extract
        return None


class WikipediaTableConnector(BaseConnector):
    name = "wikipedia_tables"
    supported_types = {"structured"}

    def is_configured(self) -> bool:
        return True

    def fetch(self, query: StructuredQuery, count: int) -> list[Item]:
        titles = _search_titles(query.search_terms(), 5)
        for title in titles:
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
            resp = requests.get(page_url, timeout=20, headers={"User-Agent": "DataFetcher/0.1"})
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
