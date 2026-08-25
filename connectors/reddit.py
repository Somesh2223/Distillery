"""Reddit connector (text) using OAuth2 application-only auth (no user login
needed for read-only search). Create a "script" app at
https://www.reddit.com/prefs/apps to get a free client id/secret."""
from __future__ import annotations

import time

import requests

from config import REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, REDDIT_USER_AGENT
from connectors.base import BaseConnector, Item, make_id
from logging_setup import get_logger, log_event
from models import StructuredQuery

logger = get_logger(__name__)

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
SEARCH_URL = "https://oauth.reddit.com/search"

_token_cache: dict = {"token": None, "expires_at": 0.0}


class RedditConnector(BaseConnector):
    name = "reddit"
    supported_types = {"text"}

    def is_configured(self) -> bool:
        return bool(REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET)

    def _get_token(self) -> str | None:
        if _token_cache["token"] and _token_cache["expires_at"] > time.time():
            return _token_cache["token"]
        try:
            resp = requests.post(
                TOKEN_URL,
                data={"grant_type": "client_credentials"},
                auth=(REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET),
                headers={"User-Agent": REDDIT_USER_AGENT},
                timeout=15,
            )
        except requests.RequestException as exc:
            log_event(logger, "reddit_token_request_failed", level=40, error=str(exc))
            return None
        if resp.status_code != 200:
            log_event(logger, "reddit_token_bad_status", level=40, status=resp.status_code)
            return None
        data = resp.json()
        token = data.get("access_token")
        _token_cache["token"] = token
        _token_cache["expires_at"] = time.time() + data.get("expires_in", 3600) - 30
        return token

    def fetch(self, query: StructuredQuery, count: int) -> list[Item]:
        token = self._get_token()
        if not token:
            return []
        items: list[Item] = []
        headers = {"Authorization": f"bearer {token}", "User-Agent": REDDIT_USER_AGENT}
        after = None
        while len(items) < count:
            params = {
                "q": query.search_terms(),
                "limit": min(100, count),
                "sort": "relevance",
                "type": "link",
            }
            if after:
                params["after"] = after
            try:
                resp = requests.get(SEARCH_URL, headers=headers, params=params, timeout=15)
            except requests.RequestException as exc:
                log_event(logger, "reddit_search_failed", level=40, error=str(exc))
                break
            if resp.status_code == 429:
                log_event(logger, "reddit_rate_limited", level=30)
                time.sleep(2)
                continue
            if resp.status_code != 200:
                log_event(logger, "reddit_bad_status", level=40, status=resp.status_code)
                break
            data = resp.json().get("data", {})
            children = data.get("children", [])
            if not children:
                break
            for child in children:
                if len(items) >= count:
                    break
                post = child.get("data", {})
                permalink = post.get("permalink")
                source_url = f"https://www.reddit.com{permalink}" if permalink else post.get("url")
                if not source_url:
                    continue
                text = post.get("selftext") or post.get("title") or ""
                items.append(
                    Item(
                        id=make_id(source_url),
                        data_type="text",
                        source_name="reddit",
                        source_url=source_url,
                        title=post.get("title"),
                        text=text,
                        license="User-generated content; subject to Reddit's terms and the poster's rights",
                        attribution=f"u/{post.get('author', 'unknown')} on r/{post.get('subreddit', '')}",
                        author=post.get("author"),
                        published_at=str(post.get("created_utc")),
                        query_text=query.search_terms(),
                        extra={"subreddit": post.get("subreddit"), "score": post.get("score")},
                    )
                )
            after = data.get("after")
            if not after:
                break
        log_event(logger, "reddit_fetch_complete", requested=count, fetched=len(items))
        return items
