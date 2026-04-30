"""Retry helper for transient Google API errors."""

import asyncio
import logging
from typing import Callable, TypeVar

from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

T = TypeVar("T")

TRANSIENT_CODES = {429, 500, 502, 503, 504}


async def retry_transient(
    fn: Callable[[], T],
    *,
    max_retries: int = 3,
    base_delay: float = 1.0,
) -> T:
    """Run *fn* with retries on transient Google API HTTP errors.

    Uses exponential backoff (1s, 2s, 4s by default).
    Raises the final HttpError if all retries are exhausted.
    Non-transient errors (4xx except 429) are raised immediately.
    """
    last_exc: HttpError | None = None
    for attempt in range(max_retries + 1):
        try:
            return await asyncio.to_thread(fn)
        except HttpError as exc:
            status = exc.resp.status if exc.resp else 0
            if status not in TRANSIENT_CODES:
                raise
            last_exc = exc
            if attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                logger.warning(
                    "Transient Google API error (HTTP %d), retry %d/%d in %.1fs: %s",
                    status, attempt + 1, max_retries, delay, exc,
                )
                await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]
