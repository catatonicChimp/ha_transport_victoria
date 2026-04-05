"""Transport Victoria vehicle tracker — device_tracker entities for live vehicle positions.

Two tracker types are supported:

1. **VehicleTrackerCoordinator / PTVVehicleTracker** (ENTRY_TYPE_VEHICLE_TRACKER)
   Linked to a parent departure-watch entry.  Follows the single vehicle
   serving that watch's next departure.

2. **RouteVehicleCoordinator / PTVRouteVehicleTracker** (ENTRY_TYPE_ROUTE_TRACKER)
   Standalone — tracks all active vehicles on a route, with optional proximity
   filtering to a reference stop.  Creates N fixed device_tracker slots (like
   departure sensor slots): slots without a vehicle show as "Away".
"""

from __future__ import annotations

import logging
import math
from datetime import timedelta
from typing import Any

import aiohttp

from homeassistant.components.device_tracker import SourceType
from homeassistant.components.device_tracker.config_entry import TrackerEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import (
    CONF_API_KEY,
    CONF_MAX_VEHICLES,
    CONF_MODE,
    CONF_PARENT_ENTRY_ID,
    CONF_PROXIMITY_RADIUS_M,
    CONF_REFERENCE_STOP_ID,
    CONF_REFERENCE_STOP_LAT,
    CONF_REFERENCE_STOP_LON,
    CONF_ROUTE_COLOR,
    CONF_ROUTE_ID,
    CONF_ROUTE_LONG_NAME,
    CONF_ROUTE_SHORT_NAME,
    CONF_WATCH_NAME,
    COORDINATOR_STAGGER_SECONDS,
    DEFAULT_MAX_VEHICLES,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DOMAIN,
    ENTRY_TYPE_ROUTE_TRACKER,
    ENTRY_TYPE_VEHICLE_TRACKER,
    FEED_VEHICLE_POSITIONS,
    MODE_MDI_ICON,
    MODE_PICTOGRAM,
    PICTOGRAM_URL_BASE,
)
from .coordinator import PTVDataUpdateCoordinator
from .realtime.client import PTVRealtimeClient
from .realtime.vehicle_positions import VehiclePosition, parse_vehicle_positions
from .rate_limiter import AsyncRateLimiter

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Platform setup — dispatches to the right setup based on entry type
# ---------------------------------------------------------------------------


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up vehicle tracker entities for this config entry."""
    entry_type = entry.data.get("entry_type", ENTRY_TYPE_VEHICLE_TRACKER)

    if entry_type == ENTRY_TYPE_ROUTE_TRACKER:
        coordinator: RouteVehicleCoordinator = entry.runtime_data
        known_trip_ids: set[str] = set()

        @callback  # type: ignore[untyped-decorator]
        def _async_add_new_vehicles() -> None:
            """Create entities for any trip_ids not yet seen."""
            vehicles: list[VehiclePosition] = coordinator.data or []
            new_entities: list[PTVRouteVehicleTracker] = []
            for pos in vehicles:
                if pos.trip_id and pos.trip_id not in known_trip_ids:
                    known_trip_ids.add(pos.trip_id)
                    new_entities.append(
                        PTVRouteVehicleTracker(coordinator, entry, pos.trip_id)
                    )
            if new_entities:
                async_add_entities(new_entities)

        entry.async_on_unload(coordinator.async_add_listener(_async_add_new_vehicles))
        _async_add_new_vehicles()
    else:
        watch_coordinator: VehicleTrackerCoordinator = entry.runtime_data
        async_add_entities([PTVVehicleTracker(watch_coordinator, entry)])


# ---------------------------------------------------------------------------
# Stop-linked tracker (existing, unchanged behaviour)
# ---------------------------------------------------------------------------


class VehicleTrackerCoordinator(DataUpdateCoordinator[VehiclePosition | None]):  # type: ignore[misc]
    """Coordinator that follows the vehicle serving the next departure.

    Reads the parent PTVDataUpdateCoordinator's data to find the current
    trip_id, then fetches the matching vehicle position from the realtime
    vehicle-positions feed.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        parent_coordinator: PTVDataUpdateCoordinator,
        entry_index: int,
    ) -> None:
        self._entry = entry
        self._parent = parent_coordinator
        self._client: PTVRealtimeClient | None = None

        # Stagger slightly behind the parent watch (+15 s) so we benefit from
        # the parent's fresh trip_id before fetching positions.
        stagger = entry_index * COORDINATOR_STAGGER_SECONDS + 15
        poll_interval = timedelta(seconds=DEFAULT_POLL_INTERVAL_SECONDS + stagger)

        super().__init__(
            hass,
            _LOGGER,
            name=f"PTV Vehicle {entry.data.get(CONF_WATCH_NAME, entry.entry_id)}",
            update_interval=poll_interval,
        )

    async def _async_setup(self) -> None:
        """Initialise the realtime client and shared rate limiter."""
        self.hass.data[DOMAIN].setdefault(
            "rate_limiter",
            AsyncRateLimiter(capacity=24.0, refill_rate=0.4),
        )
        # Get the API key from the parent entry
        parent_entry_id = self._entry.data.get(CONF_PARENT_ENTRY_ID, "")
        parent_entry = self.hass.config_entries.async_get_entry(parent_entry_id)
        if parent_entry is None:
            raise UpdateFailed("Parent watch entry not found")
        api_key: str = parent_entry.data.get(CONF_API_KEY, "")
        session = async_get_clientsession(self.hass)
        self._client = PTVRealtimeClient(api_key, session)

    async def _async_update_data(self) -> VehiclePosition | None:
        """Fetch the current position of the vehicle serving the next departure."""
        parent_data = self._parent.data
        if parent_data is None or not parent_data.departures:
            return None

        trip_id = parent_data.departures[0].trip_id
        if not trip_id:
            return None

        client = self._client
        if client is None:
            raise UpdateFailed("Vehicle tracker client not initialised")

        # Determine mode from the parent entry
        parent_entry_id = self._entry.data.get(CONF_PARENT_ENTRY_ID, "")
        parent_entry = self.hass.config_entries.async_get_entry(parent_entry_id)
        mode: str = parent_entry.data.get(CONF_MODE, "metro") if parent_entry else "metro"

        rate_limiter: AsyncRateLimiter = self.hass.data[DOMAIN]["rate_limiter"]

        try:
            await rate_limiter.acquire()
            feed = await client.fetch_feed(mode, FEED_VEHICLE_POSITIONS)
            positions = parse_vehicle_positions(feed)
            pos = positions.get(trip_id)
            if pos is None:
                _LOGGER.debug("No vehicle position found for trip_id=%s", trip_id)
            return pos
        except aiohttp.ClientResponseError as exc:
            if exc.status == 401:
                raise ConfigEntryAuthFailed("PTV API key rejected (401)") from exc
            raise UpdateFailed(f"Vehicle positions fetch failed: {exc.status}") from exc
        except aiohttp.ClientError as exc:
            raise UpdateFailed(f"Vehicle positions network error: {exc}") from exc


class PTVVehicleTracker(
    CoordinatorEntity[VehicleTrackerCoordinator],  # type: ignore[misc]
    TrackerEntity,  # type: ignore[misc]
):
    """A device_tracker entity showing the real-time position of a PTV vehicle."""

    _attr_has_entity_name = True
    _attr_source_type = SourceType.GPS
    _attr_icon = "mdi:train"

    def __init__(
        self,
        coordinator: VehicleTrackerCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_vehicle"
        self._attr_name = "Vehicle"
        mode = entry.data.get(CONF_MODE, "")
        picto = MODE_PICTOGRAM.get(mode)
        self._attr_entity_picture = f"{PICTOGRAM_URL_BASE}/{picto}.png" if picto else None

    @property
    def device_info(self) -> DeviceInfo:
        """Group under a device named after the tracker config entry."""
        watch_name = self._entry.data.get(CONF_WATCH_NAME, self._entry.entry_id)
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=f"{watch_name} Vehicle",
            manufacturer="Transport Victoria",
        )

    @property
    def latitude(self) -> float | None:
        pos = self.coordinator.data
        return pos.latitude if pos else None

    @property
    def longitude(self) -> float | None:
        pos = self.coordinator.data
        return pos.longitude if pos else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        pos = self.coordinator.data
        if pos is None:
            return {}
        return {
            "trip_id": pos.trip_id,
            "bearing": pos.bearing,
            "speed_kmh": pos.speed_kmh,
            "vehicle_id": pos.vehicle_id,
            "vehicle_label": pos.vehicle_label,
            "current_stop_id": pos.current_stop_id,
            "current_status": pos.current_status,
            "last_updated": (
                pos.timestamp.isoformat() if pos.timestamp else None
            ),
        }

    @property
    def available(self) -> bool:
        """Available once we have successfully fetched at least once."""
        return bool(self.coordinator.last_update_success)


# ---------------------------------------------------------------------------
# Route-based tracker (new)
# ---------------------------------------------------------------------------


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return the great-circle distance in metres between two WGS84 points."""
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


class RouteVehicleCoordinator(DataUpdateCoordinator[list[VehiclePosition]]):  # type: ignore[misc]
    """Coordinator tracking all active vehicles on a route.

    Fetches the vehicle-positions feed for the mode, filters by route_id,
    applies optional proximity sorting/filtering, and returns up to
    max_vehicles positions ordered by distance (if proximity filter active)
    or vehicle_label.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        entry_index: int,
    ) -> None:
        self._entry = entry
        self._client: PTVRealtimeClient | None = None

        stagger = entry_index * COORDINATOR_STAGGER_SECONDS
        poll_interval = timedelta(seconds=DEFAULT_POLL_INTERVAL_SECONDS + stagger)

        route_short_name = entry.data.get(CONF_ROUTE_SHORT_NAME, entry.entry_id)
        super().__init__(
            hass,
            _LOGGER,
            name=f"PTV Route Tracker {route_short_name}",
            update_interval=poll_interval,
        )

    async def _async_setup(self) -> None:
        """Initialise the realtime client and shared rate limiter."""
        self.hass.data[DOMAIN].setdefault(
            "rate_limiter",
            AsyncRateLimiter(capacity=24.0, refill_rate=0.4),
        )
        api_key: str = self._entry.data.get(CONF_API_KEY, "")
        session = async_get_clientsession(self.hass)
        self._client = PTVRealtimeClient(api_key, session)

    async def _async_update_data(self) -> list[VehiclePosition]:
        """Fetch and filter vehicle positions for the configured route."""
        client = self._client
        if client is None:
            raise UpdateFailed("Route tracker client not initialised")

        mode: str = self._entry.data.get(CONF_MODE, "metro")
        route_id: str = self._entry.data.get(CONF_ROUTE_ID, "")
        max_vehicles: int = int(
            self._entry.options.get(
                CONF_MAX_VEHICLES,
                self._entry.data.get(CONF_MAX_VEHICLES, DEFAULT_MAX_VEHICLES),
            )
        )

        ref_stop_id: str | None = self._entry.data.get(CONF_REFERENCE_STOP_ID)
        ref_lat: float | None = self._entry.data.get(CONF_REFERENCE_STOP_LAT)
        ref_lon: float | None = self._entry.data.get(CONF_REFERENCE_STOP_LON)
        radius_m: float = float(self._entry.data.get(CONF_PROXIMITY_RADIUS_M, 2000.0))

        rate_limiter: AsyncRateLimiter = self.hass.data[DOMAIN]["rate_limiter"]

        try:
            await rate_limiter.acquire()
            feed = await client.fetch_feed(mode, FEED_VEHICLE_POSITIONS)
        except aiohttp.ClientResponseError as exc:
            if exc.status == 401:
                raise ConfigEntryAuthFailed("PTV API key rejected (401)") from exc
            raise UpdateFailed(f"Vehicle positions fetch failed: {exc.status}") from exc
        except aiohttp.ClientError as exc:
            raise UpdateFailed(f"Vehicle positions network error: {exc}") from exc

        all_positions = parse_vehicle_positions(feed)

        # Filter to vehicles on this route (primary: route_id from feed)
        on_route = [
            pos for pos in all_positions.values()
            if pos.route_id == route_id
        ]

        # Fallback: if no vehicles matched by route_id (feed may not populate it),
        # we can't distinguish routes without a DB query — log and return empty.
        if not on_route and route_id:
            _LOGGER.debug(
                "No vehicles found with route_id=%s in feed "
                "(total positions: %d). Feed may not include route_id.",
                route_id, len(all_positions),
            )

        # Apply proximity filter if a reference stop is configured
        if ref_stop_id and ref_lat is not None and ref_lon is not None:
            with_distance: list[tuple[float, VehiclePosition]] = []
            for pos in on_route:
                dist = _haversine_m(ref_lat, ref_lon, pos.latitude, pos.longitude)
                if dist <= radius_m:
                    with_distance.append((dist, pos))
            with_distance.sort(key=lambda t: t[0])
            result = [pos for _, pos in with_distance[:max_vehicles]]
        else:
            # Sort by vehicle_label for a stable, human-readable ordering
            on_route.sort(key=lambda p: (p.vehicle_label or "", p.trip_id))
            result = on_route[:max_vehicles]

        _LOGGER.debug(
            "Route tracker %s: %d vehicles on route, %d within filter, returning %d",
            route_id, len(on_route), len(result), len(result),
        )
        return result


class PTVRouteVehicleTracker(
    CoordinatorEntity[RouteVehicleCoordinator],  # type: ignore[misc]
    TrackerEntity,  # type: ignore[misc]
):
    """A device_tracker entity for one active vehicle on a route.

    Entities are created dynamically as new trip_ids appear in the coordinator
    data.  Each entity is permanently keyed to a single trip_id; it shows its
    GPS position while that trip is active and "Away" when it drops out of the
    feed (proximity filter, trip ended, etc.).
    """

    _attr_has_entity_name = True
    _attr_source_type = SourceType.GPS

    def __init__(
        self,
        coordinator: RouteVehicleCoordinator,
        entry: ConfigEntry,
        trip_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._trip_id = trip_id
        self._attr_unique_id = f"{entry.entry_id}_{trip_id}"

        mode = entry.data.get(CONF_MODE, "")
        picto = MODE_PICTOGRAM.get(mode)
        self._attr_entity_picture = f"{PICTOGRAM_URL_BASE}/{picto}.png" if picto else None
        self._attr_icon = MODE_MDI_ICON.get(mode, "mdi:bus")

    @property
    def name(self) -> str:
        """Use vehicle_label from current coordinator data; fall back to trip_id."""
        pos = self._get_position()
        if pos and pos.vehicle_label:
            return pos.vehicle_label
        return self._trip_id

    @property
    def device_info(self) -> DeviceInfo:
        """All entities for this route share one device."""
        route_short = self._entry.data.get(CONF_ROUTE_SHORT_NAME, "")
        route_long = self._entry.data.get(CONF_ROUTE_LONG_NAME, "")
        display = route_short or route_long or self._entry.entry_id
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=f"{display} Tracker",
            manufacturer="Transport Victoria",
        )

    def _get_position(self) -> VehiclePosition | None:
        """Return the current position for this entity's trip_id, or None."""
        vehicles: list[VehiclePosition] = self.coordinator.data or []
        for pos in vehicles:
            if pos.trip_id == self._trip_id:
                return pos
        return None

    @property
    def latitude(self) -> float | None:
        pos = self._get_position()
        return pos.latitude if pos else None

    @property
    def longitude(self) -> float | None:
        pos = self._get_position()
        return pos.longitude if pos else None

    @property
    def available(self) -> bool:
        """Available once coordinator has fetched at least once."""
        return bool(self.coordinator.last_update_success)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        route_short = self._entry.data.get(CONF_ROUTE_SHORT_NAME, "")
        route_long = self._entry.data.get(CONF_ROUTE_LONG_NAME, "")
        route_color: str | None = self._entry.data.get(CONF_ROUTE_COLOR)

        base: dict[str, Any] = {
            "route_name": f"{route_short} \u2013 {route_long}" if route_long else route_short,
            "route_color": route_color,
            "trip_id": self._trip_id,
        }

        pos = self._get_position()
        if pos is None:
            return base

        base.update({
            "vehicle_id": pos.vehicle_id,
            "vehicle_label": pos.vehicle_label,
            "bearing": pos.bearing,
            "speed_kmh": pos.speed_kmh,
            "current_stop_id": pos.current_stop_id,
            "current_status": pos.current_status,
            "last_updated": pos.timestamp.isoformat() if pos.timestamp else None,
        })

        # Include distance if proximity filter is active
        ref_lat: float | None = self._entry.data.get(CONF_REFERENCE_STOP_LAT)
        ref_lon: float | None = self._entry.data.get(CONF_REFERENCE_STOP_LON)
        if ref_lat is not None and ref_lon is not None:
            base["distance_m"] = round(
                _haversine_m(ref_lat, ref_lon, pos.latitude, pos.longitude)
            )

        return base
