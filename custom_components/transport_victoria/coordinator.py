"""PTVDataUpdateCoordinator — fetches and merges static + realtime data.

One coordinator per config entry (watch). Responsibilities:
  1. Read scheduled departures from SQLite (executor thread).
  2. Fetch GTFS Realtime trip-updates and service-alerts via PTVRealtimeClient.
  3. Merge realtime delays/cancellations into the departure list.
  4. Filter alerts to those affecting the configured stop/mode.
  5. Return a CoordinatorData snapshot consumed by sensor entities.

The shared AsyncRateLimiter lives in hass.data[DOMAIN]["rate_limiter"] and is
shared across all coordinator instances so the 24 req/60 s budget is enforced
integration-wide.

Rate-limit handling (429):
  On HTTP 429 the coordinator enters an exponential backoff: the next update is
  delayed by doubling the current backoff (starting at 60 s, capped at 900 s).
  The backoff resets to zero after any successful update cycle.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any
from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

import aiohttp

from .const import (
    CONF_ALL_DESTINATIONS,
    CONF_API_KEY,
    CONF_DEPARTURE_COUNT,
    CONF_DESTINATION_STOP_ID,
    DEFAULT_DEPARTURE_COUNT,
    CONF_JOURNEY_DESTINATION_STOP_ID,
    CONF_JOURNEY_MODE,
    CONF_MIN_TRANSFER_MINUTES,
    CONF_MODE,
    CONF_PLATFORM_FILTER,
    CONF_POLL_INTERVAL,
    CONF_REALTIME_ENABLED,
    CONF_ROUTE_ID_FILTER,
    CONF_STOP_ID,
    CONF_STOP_NAME,
    CONF_TRANSFER_STOP_ID,
    CONF_TRANSFER_STOP_NAME,
    CONF_WATCH_NAME,
    COORDINATOR_STAGGER_SECONDS,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DOMAIN,
    FEED_SERVICE_ALERTS,
    FEED_TRIP_UPDATES,
    MODE_BUNDLE_MAP,
    ROUTE_TYPE_MAP,
)
from .gtfs.repository import (
    DepartureRow,
    next_departures,
    is_mode_ready,
    get_arrival_at_stop,
)
from .gtfs.time_utils import service_day_for_time
from .models import Alert, CoordinatorData, Departure, WatchConfig
from .rate_limiter import AsyncRateLimiter
from .realtime.client import PTVRealtimeClient
from .realtime.service_alerts import parse_alerts
from .realtime.trip_updates import parse_trip_updates

_LOGGER = logging.getLogger(__name__)

# Backoff constants for 429 rate-limit responses
_BACKOFF_INITIAL_SECONDS = 60
_BACKOFF_MAX_SECONDS = 900   # 15 minutes


class PTVDataUpdateCoordinator(DataUpdateCoordinator[CoordinatorData]):  # type: ignore[misc]
    """Coordinator for one PTV watch (stop + optional destination)."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        entry_index: int,
        db_path: Path,
    ) -> None:
        self._entry = entry
        self._db_path = db_path
        self._watch_config = _watch_config_from_entry(entry)
        self._client: PTVRealtimeClient | None = None
        self._backoff_seconds: int = 0  # 0 = no backoff active
        self._backoff_until: datetime | None = None
        # Diagnostics
        self.last_trip_fetch_at: datetime | None = None
        self.last_alerts_fetch_at: datetime | None = None
        self.last_http_error: int | None = None

        poll_interval = self._resolve_poll_interval(entry, entry_index)

        super().__init__(
            hass,
            _LOGGER,
            name=f"PTV {entry.data.get(CONF_WATCH_NAME, entry.entry_id)}",
            update_interval=poll_interval,
        )

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    async def _async_setup(self) -> None:
        """Initialise shared resources.

        Called once by async_config_entry_first_refresh() before the first
        _async_update_data() call. Raises ConfigEntryNotReady if the GTFS
        database is not ready yet.
        """
        bundles = MODE_BUNDLE_MAP.get(self._watch_config.mode, [])
        mode_ready = await self.hass.async_add_executor_job(
            is_mode_ready, self._db_path, bundles
        )
        if not mode_ready:
            raise ConfigEntryNotReady(
                f"GTFS data for mode '{self._watch_config.mode}' not yet imported"
            )

        # Ensure the shared rate limiter exists
        self.hass.data[DOMAIN].setdefault(
            "rate_limiter",
            AsyncRateLimiter(capacity=24.0, refill_rate=0.4),
        )

        session = async_get_clientsession(self.hass)
        self._client = PTVRealtimeClient(self._watch_config.api_key, session)

    # ------------------------------------------------------------------
    # Options update
    # ------------------------------------------------------------------

    def async_update_listeners_for_options(self) -> None:
        """React to options changes — update poll interval and watch config."""
        self._watch_config = _watch_config_from_entry(self._entry)
        new_interval = self._resolve_poll_interval(self._entry, 0)
        self.update_interval = new_interval
        _LOGGER.debug(
            "Options updated for %s: interval=%s, realtime=%s",
            self._watch_config.watch_name,
            new_interval,
            self._watch_config.realtime_enabled,
        )

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> CoordinatorData:
        """Fetch and merge realtime + static data.

        Returns a fresh CoordinatorData on success.

        Raises:
            ConfigEntryAuthFailed: On HTTP 401 (invalid/expired API key).
            UpdateFailed: On any other network or parse error.
        """
        # Respect active backoff period (from a previous 429)
        if self._backoff_until is not None and datetime.now() < self._backoff_until:
            remaining = (self._backoff_until - datetime.now()).seconds
            _LOGGER.debug("Rate-limit backoff active, skipping update (%ds remaining)", remaining)
            # Return last known data (or empty if first run)
            if self.data is not None:
                return self.data  # type: ignore[no-any-return]
            return CoordinatorData(departures=[], alerts=[])

        rate_limiter: AsyncRateLimiter = self.hass.data[DOMAIN]["rate_limiter"]
        client = self._client

        if client is None:
            raise UpdateFailed("Realtime client not initialised")

        mode = self._watch_config.mode
        realtime_enabled = self._watch_config.realtime_enabled

        # --- Trip updates ---
        trip_delays: dict[str, Any] = {}
        if realtime_enabled:
            try:
                await rate_limiter.acquire()
                trip_feed = await client.fetch_feed(mode, FEED_TRIP_UPDATES)
                trip_delays = parse_trip_updates(trip_feed)
                self.last_trip_fetch_at = datetime.now()
                self.last_http_error = None
            except aiohttp.ClientResponseError as exc:
                self.last_http_error = exc.status
                if exc.status == 401:
                    raise ConfigEntryAuthFailed("PTV API key rejected (401)") from exc
                if exc.status == 429:
                    self._handle_rate_limit()
                    _LOGGER.warning("Trip updates rate-limited (429), backing off %ds", self._backoff_seconds)
                else:
                    _LOGGER.warning("Trip updates fetch failed (%s), proceeding without realtime", exc.status)
            except aiohttp.ClientError as exc:
                _LOGGER.warning("Trip updates network error: %s", exc)

        # --- Service alerts ---
        raw_alerts: list[Alert] = []
        if realtime_enabled:
            try:
                await rate_limiter.acquire()
                alert_feed = await client.fetch_feed(mode, FEED_SERVICE_ALERTS)
                raw_alerts = parse_alerts(
                    alert_feed,
                    stop_id=self._watch_config.stop_id,
                    route_id=None,
                )
                self.last_alerts_fetch_at = datetime.now()
            except aiohttp.ClientResponseError as exc:
                self.last_http_error = exc.status
                if exc.status == 401:
                    raise ConfigEntryAuthFailed("PTV API key rejected (401)") from exc
                if exc.status == 429:
                    self._handle_rate_limit()
                    _LOGGER.warning("Alerts rate-limited (429), backing off %ds", self._backoff_seconds)
                else:
                    _LOGGER.warning("Alerts fetch failed (%s)", exc.status)
            except aiohttp.ClientError as exc:
                _LOGGER.warning("Alerts network error: %s", exc)

        # --- Scheduled departures (SQLite, executor thread) ---
        # Use HA's configured timezone so GTFS local-time comparisons are correct
        # regardless of the system/container timezone (which is typically UTC).
        local_now = dt_util.now().replace(tzinfo=None)

        watch = self._watch_config

        if watch.journey_mode and watch.transfer_stop_id and watch.journey_destination_stop_id:
            departures = await self._build_journey_departures(
                local_now, trip_delays
            )
        else:
            departures = await self._build_direct_departures(
                local_now, trip_delays
            )

        departures.sort(
            key=lambda d: d.total_journey_minutes
            if d.total_journey_minutes is not None
            else d.actual_departure.timestamp() // 60
        )

        # Successful update — reset any backoff
        self._backoff_seconds = 0
        self._backoff_until = None

        return CoordinatorData(departures=departures, alerts=raw_alerts)

    # ------------------------------------------------------------------
    # Departure builders
    # ------------------------------------------------------------------

    async def _build_direct_departures(
        self,
        local_now: datetime,
        trip_delays: dict[str, Any],
    ) -> list[Departure]:
        """Fetch scheduled departures and merge realtime delays."""
        watch = self._watch_config
        destination_stop_id: str | None = (
            None if watch.all_destinations else watch.destination_stop_id
        )

        try:
            departure_rows = await self.hass.async_add_executor_job(
                next_departures,
                self._db_path,
                watch.stop_id,
                local_now,
                watch.departure_count,
                destination_stop_id,
                watch.route_id_filter,
                watch.platform_filter,
            )
        except Exception as exc:
            raise UpdateFailed(f"Departure query failed: {exc}") from exc

        # Compute service_type labels for these trips
        service_types = await self._classify_service_types(departure_rows)

        departures: list[Departure] = []
        for row in departure_rows:
            key = f"{row.trip_id}:{row.actual_stop_id}"
            stop_delay = trip_delays.get(key)
            departures.append(
                Departure(
                    trip_id=row.trip_id,
                    route_id=row.route_id,
                    route_short_name=row.route_short_name,
                    route_long_name=row.route_long_name,
                    headsign=row.trip_headsign,
                    direction_id=row.direction_id,
                    scheduled_departure=row.scheduled_departure,
                    delay_seconds=stop_delay.delay_seconds if stop_delay else 0,
                    is_cancelled=stop_delay.is_cancelled if stop_delay else False,
                    platform=row.platform_code,
                    route_color=row.route_color,
                    service_type=service_types.get(row.trip_id),
                )
            )
        return departures

    async def _build_journey_departures(
        self,
        local_now: datetime,
        trip_delays: dict[str, Any],
    ) -> list[Departure]:
        """Build multi-leg journeys: origin→transfer, then transfer→destination."""
        watch = self._watch_config
        transfer_stop_id = watch.transfer_stop_id
        journey_dest_stop_id = watch.journey_destination_stop_id
        min_transfer_secs = watch.min_transfer_minutes * 60

        if not transfer_stop_id or not journey_dest_stop_id:
            return []

        # Leg 1: origin → transfer
        try:
            leg1_rows = await self.hass.async_add_executor_job(
                next_departures,
                self._db_path,
                watch.stop_id,
                local_now,
                watch.departure_count,
                transfer_stop_id,
                watch.route_id_filter,
                watch.platform_filter,
            )
        except Exception as exc:
            raise UpdateFailed(f"Journey leg 1 query failed: {exc}") from exc

        service_date, _ = service_day_for_time(local_now)

        journeys: list[Departure] = []
        for leg1 in leg1_rows:
            # Get arrival time at transfer stop
            arrival_at_transfer = await self.hass.async_add_executor_job(
                get_arrival_at_stop,
                self._db_path,
                leg1.trip_id,
                transfer_stop_id,
                service_date,
            )
            if arrival_at_transfer is None:
                continue

            # Apply realtime delay to arrival
            key1 = f"{leg1.trip_id}:{leg1.actual_stop_id}"
            delay1 = trip_delays.get(key1)
            delay1_secs = delay1.delay_seconds if delay1 else 0
            from datetime import timedelta as td
            adjusted_arrival = arrival_at_transfer + td(seconds=delay1_secs)
            # Earliest moment we can board leg 2
            leg2_earliest = adjusted_arrival + td(seconds=min_transfer_secs)

            # Leg 2: transfer → destination, departing after earliest
            try:
                leg2_rows = await self.hass.async_add_executor_job(
                    next_departures,
                    self._db_path,
                    transfer_stop_id,
                    leg2_earliest,
                    1,
                    journey_dest_stop_id,
                    None,
                    None,
                )
            except Exception:
                continue

            if not leg2_rows:
                continue

            leg2 = leg2_rows[0]
            key2 = f"{leg2.trip_id}:{leg2.actual_stop_id}"
            delay2 = trip_delays.get(key2)
            delay2_secs = delay2.delay_seconds if delay2 else 0
            leg2_actual = leg2.scheduled_departure + td(seconds=delay2_secs)

            # Total journey time = leg2 arrival at dest (approx) - leg1 departure
            # We don't have arrival at dest, so use leg2 departure as proxy and
            # note it in the sensor description.
            leg1_actual_dep = leg1.scheduled_departure + td(seconds=delay1_secs)
            total_mins = int((leg2_actual - leg1_actual_dep).total_seconds() // 60)

            journeys.append(
                Departure(
                    trip_id=leg1.trip_id,
                    route_id=leg1.route_id,
                    route_short_name=leg1.route_short_name,
                    route_long_name=leg1.route_long_name,
                    headsign=leg1.trip_headsign,
                    direction_id=leg1.direction_id,
                    scheduled_departure=leg1.scheduled_departure,
                    delay_seconds=delay1_secs,
                    is_cancelled=(delay1.is_cancelled if delay1 else False),
                    platform=leg1.platform_code,
                    route_color=leg1.route_color,
                    transfer_stop_name=watch.transfer_stop_name or transfer_stop_id,
                    leg2_route_name=leg2.route_short_name or leg2.route_long_name,
                    leg2_scheduled_departure=leg2.scheduled_departure,
                    leg2_actual_departure=leg2_actual,
                    total_journey_minutes=total_mins,
                )
            )

        return journeys

    async def _classify_service_types(
        self,
        rows: list[DepartureRow],
    ) -> dict[str, str]:
        """Return a map of trip_id → service_type label.

        PTV's GTFS data does not mark express/limited-express services via
        headsign or route names, and stop-count ratio heuristics produce false
        positives for Melbourne metro trains (City Loop vs direct routing adds
        2–3 extra stops without being an express pattern) and trams/buses
        (short-working trips have far fewer stops but stop at every stop along
        their path).

        Until we have a reliable signal — e.g. an explicit trip attribute or a
        verified skip-stop detection algorithm — we leave service_type as None
        for all departures so users never see an incorrect "Limited Express"
        label.
        """
        return {}

    # ------------------------------------------------------------------
    # Backoff helpers
    # ------------------------------------------------------------------

    def _handle_rate_limit(self) -> None:
        """Double the backoff window (capped at _BACKOFF_MAX_SECONDS)."""
        if self._backoff_seconds == 0:
            self._backoff_seconds = _BACKOFF_INITIAL_SECONDS
        else:
            self._backoff_seconds = min(self._backoff_seconds * 2, _BACKOFF_MAX_SECONDS)
        self._backoff_until = datetime.now() + timedelta(seconds=self._backoff_seconds)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_poll_interval(entry: ConfigEntry, entry_index: int) -> timedelta:
        """Compute the update interval from options/data, with stagger."""
        options = entry.options
        data = entry.data
        interval_s = int(options.get(
            CONF_POLL_INTERVAL,
            data.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL_SECONDS),
        ))
        stagger = entry_index * COORDINATOR_STAGGER_SECONDS
        return timedelta(seconds=interval_s + stagger)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _watch_config_from_entry(entry: ConfigEntry) -> WatchConfig:
    """Reconstruct a WatchConfig from config entry data + options."""
    data = entry.data
    options = entry.options
    return WatchConfig(
        api_key=data[CONF_API_KEY],
        mode=data[CONF_MODE],
        stop_id=data[CONF_STOP_ID],
        stop_name=data.get(CONF_STOP_NAME, ""),
        watch_name=options.get(CONF_WATCH_NAME, data.get(CONF_WATCH_NAME, "")),
        all_destinations=data.get(CONF_ALL_DESTINATIONS, True),
        destination_stop_id=data.get(CONF_DESTINATION_STOP_ID),
        gtfs_url=data.get("gtfs_url", ""),
        realtime_enabled=options.get(CONF_REALTIME_ENABLED, data.get(CONF_REALTIME_ENABLED, True)),
        departure_count=int(options.get(CONF_DEPARTURE_COUNT, data.get(CONF_DEPARTURE_COUNT, DEFAULT_DEPARTURE_COUNT))),
        route_id_filter=data.get(CONF_ROUTE_ID_FILTER),
        platform_filter=data.get(CONF_PLATFORM_FILTER),
        journey_mode=data.get(CONF_JOURNEY_MODE, False),
        transfer_stop_id=data.get(CONF_TRANSFER_STOP_ID),
        transfer_stop_name=data.get(CONF_TRANSFER_STOP_NAME, ""),
        journey_destination_stop_id=data.get(CONF_JOURNEY_DESTINATION_STOP_ID),
        min_transfer_minutes=data.get(CONF_MIN_TRANSFER_MINUTES, 5),
    )
