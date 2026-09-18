"""Structured logging for arachne. Callers must never log secret header/cookie values."""

from __future__ import annotations

import logging
import os
import sys

LOGGER_NAME = "arachne"


def setup_logging() -> None:
    level_name = os.environ.get("ARACHNE_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    if logger.handlers:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(handler)
    logger.propagate = True
