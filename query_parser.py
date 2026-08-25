"""Natural language -> StructuredQuery.

Tried in order:
1. Anthropic tool-use call that forces the model to emit a single tool call
   matching a strict JSON schema, so we never parse free-form text.
2. Google Gemini structured-output call (free tier via Google AI Studio) —
   same idea, using Gemini's response_schema instead of a forced tool call.
3. A small heuristic word-filter parser, so the app stays usable with no LLM
   key configured at all — much less accurate on unusual phrasing than
   either LLM option above.
"""
from __future__ import annotations

import json
import re
from typing import Any

from config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL, GEMINI_API_KEY, GEMINI_MODEL
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
            log_event(logger, "anthropic_parse_failed_falling_back", level=40, error=str(exc))
    if GEMINI_API_KEY:
        try:
            return _parse_with_gemini(condition)
        except Exception as exc:
            log_event(logger, "gemini_parse_failed_falling_back", level=40, error=str(exc))
    if not ANTHROPIC_API_KEY and not GEMINI_API_KEY:
        log_event(logger, "no_llm_key_using_heuristic_parser", level=30)
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


# Gemini's structured-output schema uses a different dialect than Anthropic's
# JSON Schema (uppercase type names, "nullable" instead of a ["x", "null"]
# type union), so it's expressed separately rather than reused.
_GEMINI_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "data_type": {"type": "STRING", "enum": ["image", "text", "structured"]},
        "keywords": {"type": "ARRAY", "items": {"type": "STRING"}},
        "count": {"type": "INTEGER"},
        "filters": {
            "type": "OBJECT",
            "properties": {
                "resolution": {"type": "STRING", "enum": ["low", "medium", "high"], "nullable": True},
                "orientation": {"type": "STRING", "enum": ["landscape", "portrait", "square"], "nullable": True},
                "no_watermark": {"type": "BOOLEAN"},
                "date_range": {
                    "type": "OBJECT",
                    "nullable": True,
                    "properties": {
                        "from": {"type": "STRING", "nullable": True},
                        "to": {"type": "STRING", "nullable": True},
                    },
                },
                "domain_allowlist": {"type": "ARRAY", "items": {"type": "STRING"}},
                "language": {"type": "STRING", "nullable": True},
            },
            "required": ["no_watermark", "domain_allowlist"],
        },
        "output_mode": {"type": "STRING", "enum": ["preview", "dataset"]},
        "label": {"type": "STRING"},
        "notes": {"type": "STRING", "nullable": True},
    },
    "required": ["data_type", "keywords", "count", "filters", "output_mode", "label"],
}


def _parse_with_gemini(condition: str) -> StructuredQuery:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=GEMINI_API_KEY)
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=condition,
        config=types.GenerateContentConfig(
            system_instruction=_SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=_GEMINI_RESPONSE_SCHEMA,
        ),
    )
    if not response.text:
        raise ValueError("Gemini returned an empty response")
    payload: dict[str, Any] = json.loads(response.text)
    query = StructuredQuery.model_validate(payload)
    log_event(logger, "query_parsed_by_gemini", condition=condition, structured_query=query.model_dump())
    return query


_COUNT_RE = re.compile(r"\b(\d{1,4})\b")
_IMAGE_WORDS = {"photo", "photos", "image", "images", "picture", "pictures", "pic", "pics"}
_TEXT_WORDS = {"article", "articles", "news", "post", "posts", "story", "stories"}
_STRUCTURED_WORDS = {"table", "tables", "list", "listing", "ranking", "rankings", "prices", "dataset of"}
_DATASET_WORDS = {"dataset", "training", "train", "ml", "model", "labeled", "label"}
_STOPWORDS = {
    "a", "an", "the", "of", "for", "from", "with", "no", "and", "or", "to", "in", "on",
    "high-resolution", "high", "resolution", "short", "recent", "latest",
    # conversational/request scaffolding — describes the ASK, not the SUBJECT, and
    # left unfiltered these can crowd out the actual descriptive keywords entirely
    # (e.g. "find pictures that i can give to my model to check if X" would keep
    # "find/that/i/can/give/my" instead of "X").
    "find", "finding", "give", "giving", "check", "checking", "get", "getting",
    "need", "needing", "needed", "want", "wanting", "looking", "look", "show",
    "showing", "provide", "providing", "fetch", "fetching", "help", "please",
    "can", "could", "would", "should", "will", "shall", "must", "may", "might",
    "is", "are", "was", "were", "be", "been", "being", "do", "does", "did",
    "that", "this", "these", "those", "which", "who", "whom", "whose", "what",
    "i", "im", "you", "your", "yours", "my", "mine", "me", "us", "we", "our",
    "it", "its", "not", "if", "whether", "so", "then", "just", "some", "any",
    "each", "every", "using", "used", "use", "about", "by", "few", "several",
    "many", "most", "more", "less", "without", "watermark", "watermarks",
}
# Category-indicator words are useful for detecting data_type/output_mode but
# don't describe the subject — stripped from the keywords sent to connectors
# so e.g. "model" (as in "my ML model") doesn't get searched for literally.
_NON_DESCRIPTIVE_WORDS = _IMAGE_WORDS | _TEXT_WORDS | _DATASET_WORDS | {
    w for w in _STRUCTURED_WORDS if " " not in w
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

    words = [w.strip(",.?!") for w in lower.split()]
    keywords = [
        w for w in words
        if w and w not in _STOPWORDS and w not in _NON_DESCRIPTIVE_WORDS and not w.isdigit() and not w.isnumeric()
    ]
    # A generous cap, not a truncation to the first few words — after the
    # filler-word filtering above, what's left is almost always the actual
    # subject, so we shouldn't cut it off arbitrarily.
    keywords = keywords[:15] if keywords else [condition.strip()]

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
