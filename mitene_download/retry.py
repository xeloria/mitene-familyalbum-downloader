"""Retry timing shared by page requests and media downloads.

Exponential backoff with jitter, plus support for ``Retry-After``. A server
that answers 429 or 503 with ``Retry-After: 60`` should not be hammered after
one second just because the attempt counter is low.
"""

from __future__ import annotations

import secrets
from typing import Optional

# A malicious or mistaken header must not stall a sync for hours.
MAX_RETRY_AFTER = 300.0


def backoff_delay(attempt: int, exc: Optional[BaseException] = None) -> float:
    """Seconds to wait before retry number ``attempt`` (0-based)."""
    delay = 2**attempt + secrets.SystemRandom().uniform(0, 1)

    headers = getattr(exc, "headers", None)
    if headers is not None:
        raw = headers.get("Retry-After")
        if raw:
            try:
                delay = max(delay, min(float(raw), MAX_RETRY_AFTER))
            except (TypeError, ValueError):
                # The HTTP-date form is legal but rare here; fall back to backoff.
                pass
    return delay
