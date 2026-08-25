"""Structured (JSON-lines) logging so fetch decisions are auditable:
what was fetched, from which source, and why the router chose that source.
"""
from __future__ import annotations

import json
import logging
import sys
import time


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extra = getattr(record, "extra_fields", None)
        if extra:
            payload.update(extra)
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    if root.handlers:
        return
    # On Windows, stdout defaults to the console codepage (e.g. cp1252), which
    # can't encode arbitrary unicode pulled from scraped/API text and raises
    # UnicodeEncodeError deep inside the logging handler. Force UTF-8 so any
    # fetched content is safe to log.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


_MAX_FIELD_LEN = 500


def _truncate(value):
    if isinstance(value, str) and len(value) > _MAX_FIELD_LEN:
        return value[:_MAX_FIELD_LEN] + f"...[truncated, {len(value)} chars total]"
    return value


def log_event(logger: logging.Logger, message: str, level: int = logging.INFO, **fields) -> None:
    # Some exception messages (e.g. from pandas/lxml parse errors) embed the
    # entire document they failed on, which can blow up log size — cap any
    # string field so one bad error doesn't dump megabytes into the log.
    safe_fields = {k: _truncate(v) for k, v in fields.items()}
    logger.log(level, message, extra={"extra_fields": safe_fields})
