"""Async token-bucket rate limiter for PTV Open Data API calls.

The PTV Open Data API allows 24 requests per 60 seconds across ALL endpoints
combined. This limiter is shared across all coordinator instances so the budget
is enforced integration-wide.

Usage:
    limiter = AsyncRateLimiter(capacity=24, refill_rate=0.4)
    await limiter.acquire()   # blocks until a token is available
    # make API call
"""

from __future__ import annotations

import asyncio
import logging

_LOGGER = logging.getLogger(__name__)


class AsyncRateLimiter:
    """Token-bucket rate limiter, asyncio-native.

    Parameters
    ----------
    capacity:
        Maximum burst size (tokens). The bucket starts full.
    refill_rate:
        Tokens added per second. 24 req / 60 s = 0.4 tokens/s.
    """

    def __init__(self, capacity: float = 24.0, refill_rate: float = 0.4) -> None:
        self._capacity = capacity
        self._tokens = capacity          # Start full — no burst penalty on startup
        self._refill_rate = refill_rate
        self._last_refill: float = 0.0  # asyncio monotonic time
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Wait until a token is available, then consume one.

        Refills the bucket based on elapsed wall time before checking.
        Uses asyncio.sleep (never time.sleep) so the event loop is not blocked.
        """
        async with self._lock:
            # Lazily initialise _last_refill on first call so the loop is running
            if self._last_refill == 0.0:
                self._last_refill = asyncio.get_event_loop().time()

            while True:
                now = asyncio.get_event_loop().time()
                elapsed = now - self._last_refill
                self._tokens = min(
                    self._capacity,
                    self._tokens + elapsed * self._refill_rate,
                )
                self._last_refill = now

                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return

                # Calculate how long until we have a token again
                wait_seconds = (1.0 - self._tokens) / self._refill_rate
                _LOGGER.debug(
                    "Rate limit reached — waiting %.2fs for next token", wait_seconds
                )
                # Release the lock while sleeping so other coroutines can check
                self._lock.release()
                try:
                    await asyncio.sleep(wait_seconds)
                finally:
                    await self._lock.acquire()
