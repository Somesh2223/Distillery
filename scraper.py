"""Generic fallback scraper used when no configured API connector covers the
data type, or an API returned fewer results than requested.

Discovering candidate URLs across the whole web requires a real search
backend. We deliberately do NOT scrape a search engine's results page
directly — engines like Google/DuckDuckGo actively fingerprint and block
that ("Unfortunately, bots use DuckDuckGo too."), and defeating that would
mean bypassing bot detection, which this project won't do. Instead:

- If `GOOGLE_CSE_API_KEY` + `GOOGLE_CSE_CX` are configured (Google
  Programmable Search Engine / Custom Search JSON API — free tier available),
  we use that official API to discover URLs (or, for images, direct image
  links via its image search mode).
- Independently of that, `filters.domain_allowlist` lets the scraper crawl
  specific user-named domains directly (starting from the homepage, following
  same-domain links), which needs no search API at all.
- With neither configured, the scraper has no legitimate way to find pages
  and returns nothing (logged clearly), rather than silently trying to
  circumvent a search engine's bot defenses.

Safety/compliance for whatever URLs are found either way:
- robots.txt is always checked and always respected — a domain/path that
  disallows fetching is skipped and logged, full stop. `domain_allowlist`
  only narrows/adds eligible domains; it never overrides robots.txt.
- Every URL visited (and whether it was allowed) is logged to the
  `scrape_log` table via storage.log_scrape_decision for auditability.
- Requests are rate-limited per domain, retried with backoff on 429/5xx, and
  capped at `SCRAPER_MAX_PAGES_PER_DOMAIN` pages per domain per run.
"""
from __future__ import annotations

import threading
import time
import urllib.robotparser
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from config import (
    GOOGLE_CSE_API_KEY,
    GOOGLE_CSE_CX,
    SCRAPER_MAX_PAGES_PER_DOMAIN,
    SCRAPER_MAX_RETRIES,
    SCRAPER_MIN_DELAY_SECONDS,
    SCRAPER_RENDER_JS,
    SCRAPER_USER_AGENT,
)
from connectors.base import Item, make_id, raise_for_connector_failure
from logging_setup import get_logger, log_event
from models import StructuredQuery
import storage

logger = get_logger(__name__)

GOOGLE_CSE_URL = "https://www.googleapis.com/customsearch/v1"


class _RobotsCache:
    def __init__(self) -> None:
        self._cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self._cache:
            rp = urllib.robotparser.RobotFileParser()
            rp.set_url(urljoin(origin, "/robots.txt"))
            try:
                rp.read()
            except Exception as exc:
                log_event(logger, "robots_fetch_failed_defaulting_disallow", level=30, origin=origin, error=str(exc))
                rp = None
            self._cache[origin] = rp
        rp = self._cache[origin]
        if rp is None:
            return False
        return rp.can_fetch(SCRAPER_USER_AGENT, url)


class _RateLimiter:
    def __init__(self, min_delay: float) -> None:
        self._min_delay = min_delay
        self._last_hit: dict[str, float] = {}

    def wait(self, domain: str) -> None:
        last = self._last_hit.get(domain, 0.0)
        elapsed = time.time() - last
        if elapsed < self._min_delay:
            time.sleep(self._min_delay - elapsed)
        self._last_hit[domain] = time.time()


_robots = _RobotsCache()
_rate_limiter = _RateLimiter(SCRAPER_MIN_DELAY_SECONDS)


def _domain(url: str) -> str:
    return urlparse(url).netloc


def _get_with_retry(url: str, headers: dict, params: dict | None = None) -> requests.Response | None:
    domain = _domain(url)
    backoff = 1.0
    for attempt in range(1, SCRAPER_MAX_RETRIES + 1):
        _rate_limiter.wait(domain)
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=20)
        except requests.RequestException as exc:
            log_event(logger, "scrape_request_error", level=30, url=url, attempt=attempt, error=str(exc))
            time.sleep(backoff)
            backoff *= 2
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            log_event(logger, "scrape_retryable_status", level=30, url=url, status=resp.status_code, attempt=attempt)
            retry_after = resp.headers.get("Retry-After")
            time.sleep(float(retry_after) if retry_after else backoff)
            backoff *= 2
            continue
        return resp
    return None


def _check_allowed(url: str) -> bool:
    domain = _domain(url)
    allowed = _robots.allowed(url)
    storage.log_scrape_decision(domain, url, allowed, reason="" if allowed else "disallowed by robots.txt")
    if not allowed:
        log_event(logger, "scrape_skipped_robots_disallowed", level=30, domain=domain, url=url)
    return allowed


def _google_cse_urls(term: str, limit: int, image: bool) -> list[str]:
    """Official, ToS-compliant web search via Google Programmable Search
    Engine. Returns page URLs, or (image=True) direct image URLs."""
    if not (GOOGLE_CSE_API_KEY and GOOGLE_CSE_CX):
        return []
    urls: list[str] = []
    start = 1
    while len(urls) < limit and start <= 91:
        params = {
            "key": GOOGLE_CSE_API_KEY,
            "cx": GOOGLE_CSE_CX,
            "q": term,
            "num": min(10, limit - len(urls)),
            "start": start,
        }
        if image:
            params["searchType"] = "image"
        try:
            resp = requests.get(GOOGLE_CSE_URL, params=params, timeout=15)
        except requests.RequestException as exc:
            log_event(logger, "google_cse_request_failed", level=40, error=str(exc))
            if not urls:
                raise_for_connector_failure("Google Custom Search", exc=exc)
            break
        if resp.status_code == 429:
            time.sleep(2)
            continue
        if resp.status_code != 200:
            log_event(logger, "google_cse_bad_status", level=40, status=resp.status_code, body=resp.text[:300])
            if not urls:
                raise_for_connector_failure("Google Custom Search", status_code=resp.status_code)
            break
        entries = resp.json().get("items", [])
        if not entries:
            break
        for entry in entries:
            link = entry.get("link")
            if link:
                urls.append(link)
        start += len(entries)
    return urls[:limit]


def _extract_article_text(soup: BeautifulSoup) -> str:
    article = soup.find("article")
    if article:
        return article.get_text(" ", strip=True)
    paragraphs = soup.find_all("p")
    return " ".join(p.get_text(" ", strip=True) for p in paragraphs)


def fallback_fetch(
    query: StructuredQuery,
    count: int,
    allowed_domains: list[str] | None = None,
    cancel_event: threading.Event | None = None,
) -> list[Item]:
    """Discover + scrape a fallback set of items when API connectors can't
    fully satisfy the request. Uses Google CSE (if configured) for web-wide
    discovery and/or a same-domain crawl of `domain_allowlist` (if given).
    Neither bypasses robots.txt or any site's bot defenses."""
    terms = query.search_terms()
    if not terms:
        return []

    domain_filter = [d.lower() for d in (allowed_domains or query.filters.domain_allowlist or [])]
    headers = {"User-Agent": SCRAPER_USER_AGENT}
    items: list[Item] = []
    pages_per_domain: dict[str, int] = {}

    search_urls = _google_cse_urls(terms, max(count * 3, 10), image=(query.data_type == "image"))
    if domain_filter:
        search_urls = [u for u in search_urls if _domain(u).lower() in domain_filter or any(_domain(u).lower().endswith("." + d) for d in domain_filter)]

    for url in search_urls:
        if len(items) >= count or (cancel_event is not None and cancel_event.is_set()):
            break
        domain = _domain(url)

        if query.data_type == "image":
            # Google image search already gives us the direct image URL.
            if not _check_allowed(url):
                continue
            items.append(
                Item(
                    id=make_id(url),
                    data_type="image",
                    source_name=f"scraper:{domain}",
                    source_url=url,
                    license="Unknown — verify the site's terms before reuse",
                    attribution=domain,
                    query_text=terms,
                )
            )
            continue

        if pages_per_domain.get(domain, 0) >= SCRAPER_MAX_PAGES_PER_DOMAIN:
            continue
        if not _check_allowed(url):
            continue
        resp = _get_with_retry(url, headers)
        pages_per_domain[domain] = pages_per_domain.get(domain, 0) + 1
        if resp is None or resp.status_code != 200 or "html" not in resp.headers.get("Content-Type", ""):
            continue
        soup = BeautifulSoup(resp.text, "html.parser")
        if query.data_type == "text":
            title = soup.title.get_text(strip=True) if soup.title else url
            text = _extract_article_text(soup)
            if text:
                items.append(
                    Item(
                        id=make_id(url),
                        data_type="text",
                        source_name=f"scraper:{domain}",
                        source_url=url,
                        title=title,
                        text=text,
                        license="Unknown — verify the site's terms before reuse",
                        attribution=domain,
                        query_text=terms,
                    )
                )
        elif query.data_type == "structured":
            items.extend(_scrape_tables(resp.text, url, domain, terms, count - len(items)))

    if len(items) < count and domain_filter:
        for domain in domain_filter:
            if len(items) >= count or (cancel_event is not None and cancel_event.is_set()):
                break
            items.extend(_crawl_domain(domain, query, count - len(items), headers, pages_per_domain, cancel_event))

    if not search_urls and not domain_filter:
        log_event(
            logger,
            "no_search_backend_available",
            level=30,
            reason="set GOOGLE_CSE_API_KEY + GOOGLE_CSE_CX for web-wide discovery, "
            "or provide filters.domain_allowlist to crawl specific sites directly",
        )

    log_event(logger, "scraper_fallback_complete", requested=count, fetched=len(items), pages_per_domain=pages_per_domain)
    return items[:count]


def _crawl_domain(
    domain: str,
    query: StructuredQuery,
    remaining: int,
    headers: dict,
    pages_per_domain: dict[str, int],
    cancel_event: threading.Event | None = None,
) -> list[Item]:
    """Same-domain BFS crawl starting at the homepage — used when the user
    explicitly allowlists a domain, independent of any search API."""
    items: list[Item] = []
    to_visit = [f"https://{domain}/"]
    visited: set[str] = set()
    terms = query.search_terms()

    while to_visit and len(items) < remaining and pages_per_domain.get(domain, 0) < SCRAPER_MAX_PAGES_PER_DOMAIN:
        if cancel_event is not None and cancel_event.is_set():
            break
        url = to_visit.pop(0)
        if url in visited:
            continue
        visited.add(url)
        if not _check_allowed(url):
            continue
        resp = _get_with_retry(url, headers)
        pages_per_domain[domain] = pages_per_domain.get(domain, 0) + 1
        if resp is None or resp.status_code != 200 or "html" not in resp.headers.get("Content-Type", ""):
            continue
        soup = BeautifulSoup(resp.text, "html.parser")

        if query.data_type == "image":
            items.extend(_scrape_images(soup, url, domain, query, remaining - len(items)))
        elif query.data_type == "text":
            title = soup.title.get_text(strip=True) if soup.title else url
            text = _extract_article_text(soup)
            if text:
                items.append(
                    Item(
                        id=make_id(url),
                        data_type="text",
                        source_name=f"scraper:{domain}",
                        source_url=url,
                        title=title,
                        text=text,
                        license="Unknown — verify the site's terms before reuse",
                        attribution=domain,
                        query_text=terms,
                    )
                )
        elif query.data_type == "structured":
            items.extend(_scrape_tables(resp.text, url, domain, terms, remaining - len(items)))

        if len(items) < remaining:
            for a in soup.find_all("a", href=True):
                abs_url = urljoin(url, a["href"])
                if _domain(abs_url) == domain and abs_url.startswith("http") and abs_url not in visited:
                    to_visit.append(abs_url)

    return items


def _scrape_images(soup: BeautifulSoup, page_url: str, domain: str, query: StructuredQuery, remaining: int) -> list[Item]:
    items: list[Item] = []
    for img in soup.find_all("img"):
        if len(items) >= remaining:
            break
        src = img.get("src") or img.get("data-src")
        if not src:
            continue
        abs_url = urljoin(page_url, src)
        if not abs_url.startswith("http") or not _check_allowed(abs_url):
            continue
        alt = img.get("alt", "")
        items.append(
            Item(
                id=make_id(abs_url),
                data_type="image",
                source_name=f"scraper:{domain}",
                source_url=abs_url,
                title=alt or None,
                license="Unknown — verify the site's terms before reuse",
                attribution=domain,
                query_text=query.search_terms(),
            )
        )
    return items


def _scrape_tables(html: str, page_url: str, domain: str, terms: str, remaining: int) -> list[Item]:
    try:
        import pandas as pd
    except ImportError:
        return []
    try:
        import io

        tables = pd.read_html(io.StringIO(html))
    except ValueError:
        return []
    if not tables:
        return []
    best = max(tables, key=lambda df: df.shape[0] * df.shape[1]).head(remaining)
    best.columns = [str(c) for c in best.columns]
    items: list[Item] = []
    import json as _json

    for i, row in best.iterrows():
        row_dict = {str(k): (None if pd.isna(v) else str(v)) for k, v in row.items()}
        row_url = f"{page_url}#row-{i}"
        items.append(
            Item(
                id=make_id(row_url),
                data_type="structured",
                source_name=f"scraper:{domain}",
                source_url=page_url,
                title=f"row {i} from {domain}",
                text=_json.dumps(row_dict, ensure_ascii=False),
                license="Unknown — verify the site's terms before reuse",
                attribution=domain,
                query_text=terms,
                extra={"columns": list(best.columns), "row": row_dict},
            )
        )
    return items


def render_with_playwright(url: str) -> str | None:
    """Optional JS-rendering path for pages that need it. Only used when
    SCRAPER_RENDER_JS=true and the `playwright` package is installed."""
    if not SCRAPER_RENDER_JS:
        return None
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log_event(logger, "playwright_not_installed", level=30)
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(user_agent=SCRAPER_USER_AGENT)
            page.goto(url, timeout=20000)
            html = page.content()
            browser.close()
            return html
    except Exception as exc:
        log_event(logger, "playwright_render_failed", level=40, url=url, error=str(exc))
        return None
