"""Runtime settings for the P0 extract pipeline."""

from __future__ import annotations

import os

CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 15.0
MAX_REDIRECTS = 20
MAX_BODY_BYTES = 2 * 1024 * 1024
MAIN_TEXT_MAX_CHARS = 100_000
MAX_LINKS = 50

USER_AGENT = os.environ.get(
    "ARACHNE_USER_AGENT",
    "Arachne/0.1 (+https://github.com/Ever12349/arachne)",
)
