"""Structured logging for arachne. Callers must never log secret header/cookie values."""

from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from contextvars import ContextVar

from app import config

LOGGER_NAME = "arachne"
REQUEST_ID_HEADER = "X-Request-Id"
_MAX_REQUEST_ID_LEN = 128

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


def resolve_request_id(raw: str | None) -> str:
    """Use a caller-supplied X-Request-Id, or generate a UUID4."""
    if raw is None:
        return str(uuid.uuid4())
    value = raw.strip()
    if not value or len(value) > _MAX_REQUEST_ID_LEN or "\n" in value or "\r" in value:
        return str(uuid.uuid4())
    return value


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get("-")
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line. Does not include request headers or cookies."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", None) or request_id_var.get("-"),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging() -> None:
    level_name = os.environ.get("ARACHNE_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    if logger.handlers:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(RequestIdFilter())
    if config.LOG_JSON:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(handler)
    logger.propagate = True
