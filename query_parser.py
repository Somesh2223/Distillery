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
from datetime import date, timedelta
from typing import Any, Optional

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

_SYSTEM_PROMPT_TEMPLATE = (
    "Today's date is {today}. Resolve every relative time expression (\"the last 6 months\", "
    "\"recent\", \"since last year\") against that date, not against your training data — a "
    "date_range anchored to the wrong year silently filters out every matching result. "
    "You convert a user's natural-language data request into a structured query for a data-fetching "
    "tool. Infer sensible defaults when the user doesn't specify something explicitly: default count "
    "is 20 if not stated (cap at 1000), default output_mode is 'preview' unless the user clearly wants "
    "a training/testing dataset (mentions of 'dataset', 'training', 'ML', 'labeled', 'train/test split' "
    "imply output_mode='dataset'). For data_type='image', phrase each keyword as a literal, concrete "
    "visual scene or texture the camera would actually see (e.g. 'icy sidewalk', 'wet tile floor close up', "
    "'rain-soaked pavement') rather than the abstract concept itself (e.g. avoid bare words like 'slippery' "
    "or 'danger') — stock photo libraries tag abstract hazard concepts overwhelmingly with warning-sign and "
    "caution-icon photos, not photos of the actual surface/condition, so concrete scene descriptions match "
    "real, relevant photos far more often. Only include sign/warning/icon imagery in the keywords if the "
    "user explicitly asked for that. Always call the emit_structured_query tool exactly once."
)


def _system_prompt() -> str:
    # Built per call, not once at import: a long-running server would otherwise
    # keep telling the model it's still whatever day the process started on.
    return _SYSTEM_PROMPT_TEMPLATE.format(today=date.today().isoformat())


def parse_condition(condition: str) -> StructuredQuery:
    fallback_reason: str | None = None
    if ANTHROPIC_API_KEY:
        try:
            return _parse_with_llm(condition)
        except Exception as exc:
            log_event(logger, "anthropic_parse_failed_falling_back", level=40, error=str(exc))
            fallback_reason = f"Anthropic call failed ({exc}); "
    if GEMINI_API_KEY:
        try:
            return _parse_with_gemini(condition)
        except Exception as exc:
            log_event(logger, "gemini_parse_failed_falling_back", level=40, error=str(exc))
            fallback_reason = (fallback_reason or "") + f"Gemini call failed ({exc}); "
    if not ANTHROPIC_API_KEY and not GEMINI_API_KEY:
        log_event(logger, "no_llm_key_using_heuristic_parser", level=30)
        fallback_reason = "no ANTHROPIC_API_KEY or GEMINI_API_KEY configured; "
    return _heuristic_parse(condition, fallback_reason)


def _parse_with_llm(condition: str) -> StructuredQuery:
    import anthropic

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=1024,
        system=_system_prompt(),
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
            system_instruction=_system_prompt(),
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


_IMAGE_WORDS = {"photo", "photos", "image", "images", "picture", "pictures", "pic", "pics"}
_TEXT_WORDS = {"article", "articles", "news", "post", "posts", "story", "stories", "blog", "blogs"}
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
    "taken", "ago", "between", "during", "within", "over", "under",
}
# Category-indicator words are useful for detecting data_type/output_mode but
# don't describe the subject — stripped from the keywords sent to connectors
# so e.g. "model" (as in "my ML model") doesn't get searched for literally.
_NON_DESCRIPTIVE_WORDS = _IMAGE_WORDS | _TEXT_WORDS | _DATASET_WORDS | {
    w for w in _STRUCTURED_WORDS if " " not in w
}

# --- time expressions ---
# Pulled out of the condition before anything else looks at it. Their digits
# would otherwise be read as the requested item count ("the last 6 months" ->
# fetch 6 items, "since 2020" -> fetch 1000) and their words would end up in
# the search keywords, where they match nothing.
_UNIT_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}
_RELATIVE_PERIOD_RE = re.compile(
    r"\b(?:in|from|over|within|during)?\s*(?:the\s+)?(?:last|past|previous)\s+(?:(\d{1,3})\s+)?(day|week|month|year)s?\b"
)
_SINCE_YEAR_RE = re.compile(r"\b(?:since|after)\s+((?:19|20)\d{2})\b")

# A bare number is only a count when a countable noun follows it fairly
# closely ("500 wet road surface photos"). This is what keeps model numbers
# and years in the subject ("Boeing 747", "the 2008 financial crisis") from
# being swallowed as counts.
_COUNTABLE_NOUNS = sorted(
    _IMAGE_WORDS | _TEXT_WORDS | {
        "table", "tables", "listing", "listings", "ranking", "rankings",
        "item", "items", "result", "results", "sample", "samples",
        "example", "examples", "row", "rows", "entry", "entries",
    },
    key=len,
    reverse=True,
)
_COUNT_RE = re.compile(
    r"\b(\d{1,4})\s+(?:[a-z][\w'-]*\s+){0,4}?(?:" + "|".join(_COUNTABLE_NOUNS) + r")\b"
)
_LEADING_COUNT_RE = re.compile(r"^\s*(\d{1,4})\b")


def _extract_date_range(text: str) -> tuple[Optional[dict], str]:
    """Returns (date_range, text with the time expression removed).

    Month and year lengths are approximated in days. This is the no-LLM
    fallback path, where being a couple of days off on a range boundary
    matters far less than not mangling the count and keywords.
    """
    today = date.today()

    m = _RELATIVE_PERIOD_RE.search(text)
    if m:
        n = int(m.group(1)) if m.group(1) else 1
        start = today - timedelta(days=n * _UNIT_DAYS[m.group(2)])
        return (
            {"from": start.isoformat(), "to": today.isoformat()},
            text[:m.start()] + " " + text[m.end():],
        )

    m = _SINCE_YEAR_RE.search(text)
    if m:
        return (
            {"from": f"{m.group(1)}-01-01", "to": today.isoformat()},
            text[:m.start()] + " " + text[m.end():],
        )

    return None, text


def _heuristic_parse(condition: str, fallback_reason: str | None = None) -> StructuredQuery:
    lower = condition.lower()
    date_range, cleaned = _extract_date_range(lower)

    data_type = "text"
    if any(w in lower for w in _IMAGE_WORDS):
        data_type = "image"
    elif any(w in lower for w in _STRUCTURED_WORDS):
        data_type = "structured"
    elif any(w in lower for w in _TEXT_WORDS):
        data_type = "text"

    count_match = _COUNT_RE.search(cleaned) or _LEADING_COUNT_RE.search(cleaned)
    count = int(count_match.group(1)) if count_match else 20
    count = max(1, min(count, 1000))
    # Only the digits actually read as the count are dropped from the
    # keywords — other numbers are usually part of the subject itself
    # ("Boeing 747", "2008 financial crisis") and searching without them
    # finds the wrong thing.
    count_token = count_match.group(1) if count_match else None

    words = [w.strip(",.?!").lstrip("-+") for w in cleaned.split()]
    keywords = [
        w for w in words
        if w and w not in _STOPWORDS and w not in _NON_DESCRIPTIVE_WORDS and w != count_token
    ]
    if not keywords:
        # The condition names no subject at all ("articles from last month").
        # Keep the category words rather than falling back to the raw
        # sentence — that would put the time expression we just stripped
        # back into the search string as one long unmatchable phrase.
        keywords = [w for w in words if w and w not in _STOPWORDS and w != count_token]
    # A generous cap, not a truncation to the first few words — after the
    # filler-word filtering above, what's left is almost always the actual
    # subject, so we shouldn't cut it off arbitrarily.
    keywords = keywords[:15]

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
            "date_range": date_range,
            "domain_allowlist": [],
        },
        output_mode=output_mode,  # type: ignore[arg-type]
        label=label,
        notes=f"parsed heuristically — {fallback_reason or 'reason unknown'}".strip(),
    )
    log_event(logger, "query_parsed_heuristically", condition=condition, structured_query=query.model_dump())
    return query
