"""PTV GTFS Realtime client.

Fetches and decodes protobuf feeds from the PTV Open Data API.

Key design choices:
- Module-level TTL cache keyed by (mode, feed_type) so multiple coordinator
  instances watching the same mode share one HTTP request per poll cycle.
- The shared AsyncRateLimiter (from rate_limiter.py) must be passed in by
  the caller — it lives in hass.data[DOMAIN]["rate_limiter"].
- Each PTVRealtimeClient instance holds an aiohttp ClientSession reference
  obtained from homeassistant.helpers.aiohttp_client — do not create sessions
  directly as HA manages their lifecycle.
- Protobuf decoding (_decode_feed) and the google.transit import are both
  run in a thread-pool executor because importing the C extension
  (google._upb._message) calls importlib.import_module, which blocks the
  event loop and triggers HA's blocking-call detector.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import aiohttp

from ..const import (
    REALTIME_AUTH_HEADER,
    REALTIME_BASE_URL,
    REALTIME_CACHE_TTL_SECONDS,
)

if TYPE_CHECKING:
    from google.transit import gtfs_realtime_pb2 as gtfs_rt

_LOGGER = logging.getLogger(__name__)

# Module-level cache: {(mode, feed_type): (FeedMessage, fetched_at)}
_FEED_CACHE: dict[
    tuple[str, str],
    tuple[object, datetime],
] = {}

_CACHE_TTL = timedelta(seconds=REALTIME_CACHE_TTL_SECONDS)


class PTVRealtimeClient:
    """Client for the PTV GTFS Realtime API."""

    def __init__(self, api_key: str, session: aiohttp.ClientSession) -> None:
        self._api_key = api_key
        self._session = session

    # ------------------------------------------------------------------
    # Feed fetching
    # ------------------------------------------------------------------

    async def fetch_feed(self, mode: str, feed_type: str) -> object:
        """Fetch and decode a GTFS Realtime protobuf feed.

        Returns a cached FeedMessage if the TTL has not expired, avoiding
        redundant HTTP requests when multiple coordinators poll the same mode.

        Protobuf decoding runs in a thread-pool executor to avoid blocking
        the event loop during C-extension import and binary parsing.

        Args:
            mode: One of the MODE_* constants (e.g. "metro").
            feed_type: One of the FEED_* constants (e.g. "trip-updates").

        Returns:
            A decoded gtfs_realtime_pb2.FeedMessage instance.

        Raises:
            aiohttp.ClientResponseError: On HTTP errors (including 401).
            aiohttp.ClientError: On network errors.
        """
        cache_key = (mode, feed_type)
        now = datetime.now(tz=timezone.utc)

        cached_feed, cached_at = _FEED_CACHE.get(cache_key, (None, None))
        if cached_feed is not None and cached_at is not None:
            if now - cached_at < _CACHE_TTL:
                _LOGGER.debug("Cache hit for %s/%s", mode, feed_type)
                return cached_feed

        url = f"{REALTIME_BASE_URL}/{mode}/{feed_type}"
        _LOGGER.debug("Fetching realtime feed: %s", url)

        async with self._session.get(
            url,
            headers={REALTIME_AUTH_HEADER: self._api_key},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            response.raise_for_status()
            content = await response.read()

        # Decode protobuf in executor — importing the C extension and parsing
        # binary data both block; running in a thread keeps the event loop free.
        loop = asyncio.get_running_loop()
        feed = await loop.run_in_executor(None, _decode_feed, content)

        _FEED_CACHE[cache_key] = (feed, now)
        return feed

    # ------------------------------------------------------------------
    # API key validation
    # ------------------------------------------------------------------

    async def test_connection(self) -> None:
        """Validate the API key by fetching the metro trip-updates feed.

        Raises:
            aiohttp.ClientResponseError: On 401 (invalid key) or other HTTP errors.
            aiohttp.ClientError: On network errors.
        """
        url = f"{REALTIME_BASE_URL}/metro/trip-updates"
        async with self._session.get(
            url,
            headers={REALTIME_AUTH_HEADER: self._api_key},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as response:
            response.raise_for_status()
            await response.read()


def _decode_feed(content: bytes) -> object:
    """Decode raw protobuf bytes into a FeedMessage.

    Designed to run in a thread-pool executor — importing google.transit
    triggers a blocking C-extension import the first time it is called.
    """
    from google.transit import gtfs_realtime_pb2

    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(content)
    return feed


def invalidate_cache(mode: str | None = None, feed_type: str | None = None) -> None:
    """Invalidate one or all cache entries.

    Called when we know the cache is stale (e.g. after a config change).
    If mode and feed_type are both None, clears the entire cache.
    """
    if mode is None and feed_type is None:
        _FEED_CACHE.clear()
    else:
        if mode is not None and feed_type is not None:
            _FEED_CACHE.pop((mode, feed_type), None)
