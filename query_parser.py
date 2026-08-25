"""Natural language -> StructuredQuery.

Primary path: an Anthropic tool-use call that forces the model to emit a
single tool call matching a strict JSON schema, so we never have to parse
free-form text out of the response.

Fallback path (no ANTHROPIC_API_KEY configured): a small heuristic parser so
the app remains usable for demos/tests without an LLM key.
"""
from __future__ import annotations

import json
import re
from typing import Any

from config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL
from logging_setup import get_logger, log_event
from models import StructuredQuery

logger = get_logger(__name__)

_TOOL_SCHEMA = {
    "name": "emit_structured_query",
    "description": "Emit the structured interpretation of the user's data-fetching request.",
    "input_schema": {
        "type": "object",
        "properties": {
            "data_type": {
                "type": "string",
                "enum": ["image", "text", "structured"],
                "description": "image = photos/pictures; text = articles/posts/news; structured = tables/listings/rankings/prices.",
            },
            "keywords": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Core search terms/subjects, stripped of counts and filter phrases, e.g. ['red sports car', 'side view'].",
            },
            "count": {"type": "integer", "minimum": 1, "maximum": 1000},
            "filters": {
                "type": "object",
                "properties": {
                    "resolution": {"type": ["string", "null"], "enum": ["low", "medium", "high", None]},
                    "orientation": {"type": ["string", "null"], "enum": ["landscape", "portrait", "square", None]},
                    "no_watermark": {"type": "boolean"},
                    "date_range": {
                        "type": ["object", "null"],
                        "properties": {
                            "from": {"type": ["string", "null"], "description": "ISO date or null"},
                            "to": {"type": ["string", "null"], "description": "ISO date or null"},
                        },
                    },
                    "domain_allowlist": {"type": "array", "items": {"type": "string"}},
                    "language": {"type": ["string", "null"], "description": "ISO 639-1 code, e.g. 'en'"},
                },
                "required": ["no_watermark", "domain_allowlist"],
            },
            "output_mode": {"type": "string", "enum": ["preview", "dataset"]},
            "label": {
                "type": "string",
                "description": "Short snake_case label for this class/category, used as the dataset folder/category name, e.g. 'red_sports_cars'.",
            },
            "notes": {"type": ["string", "null"], "description": "Anything else worth preserving that doesn't fit the fields above."},
        },
        "required": ["data_type", "keywords", "count", "filters", "output_mode", "label"],
    },
}

_SYSTEM_PROMPT = (
    "You convert a user's natural-language data request into a structured query for a data-fetching "
    "tool. Infer sensible defaults when the user doesn't specify something explicitly: default count "
    "is 20 if not stated (cap at 1000), default output_mode is 'preview' unless the user clearly wants "
    "a training/testing dataset (mentions of 'dataset', 'training', 'ML', 'labeled', 'train/test split' "
    "imply output_mode='dataset'). Always call the emit_structured_query tool exactly once."
)


def parse_condition(condition: str) -> StructuredQuery:
    if ANTHROPIC_API_KEY:
        try:
            return _parse_with_llm(condition)
        except Exception as exc:
            log_event(logger, "llm_parse_failed_falling_back", level=40, error=str(exc))
    else:
        log_event(logger, "no_anthropic_key_using_heuristic_parser", level=30)
    return _heuristic_parse(condition)


def _parse_with_llm(condition: str) -> StructuredQuery:
    import anthropic

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=1024,
        system=_SYSTEM_PROMPT,
        tools=[_TOOL_SCHEMA],
        tool_choice={"type": "tool", "name": "emit_structured_query"},
        messages=[{"role": "user", "content": condition}],
    )
    tool_use = next((b for b in response.content if b.type == "tool_use"), None)
    if tool_use is None:
        raise ValueError("model did not return a tool_use block")
    payload: dict[str, Any] = tool_use.input
    query = StructuredQuery.model_validate(payload)
    log_event(logger, "query_parsed_by_llm", condition=condition, structured_query=query.model_dump())
    return query


_COUNT_RE = re.compile(r"\b(\d{1,4})\b")
_IMAGE_WORDS = {"photo", "photos", "image", "images", "picture", "pictures", "pic", "pics"}
_TEXT_WORDS = {"article", "articles", "news", "post", "posts", "story", "stories"}
_STRUCTURED_WORDS = {"table", "tables", "list", "listing", "ranking", "rankings", "prices", "dataset of"}
_DATASET_WORDS = {"dataset", "training", "train", "ml", "model", "labeled", "label"}
_STOPWORDS = {
    "a", "an", "the", "of", "for", "from", "with", "no", "and", "or", "to", "in", "on",
    "high-resolution", "high", "resolution", "short", "recent", "latest",
}


def _heuristic_parse(condition: str) -> StructuredQuery:
    lower = condition.lower()

    data_type = "text"
    if any(w in lower for w in _IMAGE_WORDS):
        data_type = "image"
    elif any(w in lower for w in _STRUCTURED_WORDS):
        data_type = "structured"
    elif any(w in lower for w in _TEXT_WORDS):
        data_type = "text"

    count_match = _COUNT_RE.search(condition)
    count = int(count_match.group(1)) if count_match else 20
    count = max(1, min(count, 1000))

    words = [w.strip(",.") for w in lower.split()]
    keywords = [w for w in words if w not in _STOPWORDS and not w.isdigit()]
    # collapse to a short phrase rather than a long stopword-stripped bag
    keywords = keywords[:8] if keywords else [condition.strip()]

    no_watermark = "no watermark" in lower or "without watermark" in lower
    orientation = None
    for o in ("landscape", "portrait", "square"):
        if o in lower:
            orientation = o
    resolution = "high" if "high-resolution" in lower or "high resolution" in lower or "hd" in lower else None

    output_mode = "dataset" if any(w in lower for w in _DATASET_WORDS) else "preview"

    label_source = keywords[:3] if keywords else ["dataset"]
    label = "_".join(re.sub(r"[^a-z0-9]+", "", w) for w in label_source).strip("_") or "dataset"

    query = StructuredQuery(
        data_type=data_type,  # type: ignore[arg-type]
        keywords=keywords,
        count=count,
        filters={
            "resolution": resolution,
            "orientation": orientation,
            "no_watermark": no_watermark,
            "domain_allowlist": [],
        },
        output_mode=output_mode,  # type: ignore[arg-type]
        label=label,
        notes="parsed heuristically (no ANTHROPIC_API_KEY set)",
    )
    log_event(logger, "query_parsed_heuristically", condition=condition, structured_query=query.model_dump())
    return query
