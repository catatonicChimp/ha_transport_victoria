"""Data models for the PTV integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class WatchConfig:
    """Represents one user-configured watch (a config entry).

    A watch is a stop (+ optional destination) that the user wants departure
    sensors for. One config entry = one watch = 3 departure sensors + 1
    disruption binary sensor.
    """

    api_key: str
    mode: str                            # One of const.MODES
    stop_id: str
    stop_name: str
    watch_name: str
    all_destinations: bool = True
    destination_stop_id: str | None = None
    destination_stop_name: str | None = None
    gtfs_url: str = ""                   # Direct ZIP download URL
    realtime_enabled: bool = True        # False disables RT fetches (saves API quota)
    departure_count: int = 3             # Number of next-service sensors to create

    # Filtering
    route_id_filter: str | None = None   # Restrict to a specific route/line
    platform_filter: str | None = None  # Restrict to a specific platform code

    # Multi-leg journey mode
    journey_mode: bool = False
    transfer_stop_id: str | None = None
    transfer_stop_name: str | None = None
    journey_destination_stop_id: str | None = None
    journey_destination_stop_name: str | None = None
    min_transfer_minutes: int = 5


@dataclass(slots=True)
class Departure:
    """A departure from a stop, with optional realtime enrichment."""

    trip_id: str
    route_id: str
    route_short_name: str
    route_long_name: str
    headsign: str
    direction_id: int | None
    scheduled_departure: datetime        # Absolute local datetime
    delay_seconds: int = 0              # From realtime trip-updates feed
    is_cancelled: bool = False
    platform: str | None = None
    route_color: str | None = None     # Hex colour string from routes table (no '#')
    service_type: str | None = None    # "Stopping All Stations" / "Limited Express" / "Express"

    # Multi-leg journey fields (populated only in journey mode)
    transfer_stop_name: str | None = None
    leg2_route_name: str | None = None
    leg2_scheduled_departure: datetime | None = None
    leg2_actual_departure: datetime | None = None
    total_journey_minutes: int | None = None

    @property
    def actual_departure(self) -> datetime:
        """Return the realtime-adjusted departure time."""
        from datetime import timedelta
        return self.scheduled_departure + timedelta(seconds=self.delay_seconds)

    @property
    def minutes_until(self) -> int:
        """Minutes until actual departure from now (can be negative if passed)."""
        from datetime import datetime as dt
        delta = self.actual_departure - dt.now()
        return int(delta.total_seconds() // 60)


@dataclass(slots=True)
class Alert:
    """A service alert from the GTFS Realtime service-alerts feed."""

    alert_id: str
    header: str
    description: str
    cause: str = "UNKNOWN_CAUSE"
    effect: str = "UNKNOWN_EFFECT"
    active_from: datetime | None = None
    active_until: datetime | None = None

    # Scope — which routes/stops this alert affects
    affected_route_ids: list[str] = field(default_factory=list)
    affected_stop_ids: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RouteTrackerConfig:
    """Configuration for a route-based vehicle tracker entry.

    Tracks all active vehicles on a route, optionally filtered to those
    within a radius of a reference stop.
    """

    api_key: str
    mode: str                          # One of const.MODES
    route_id: str
    route_short_name: str
    route_long_name: str
    tracker_name: str
    route_color: str | None = None     # hex without '#', from GTFS routes table
    max_vehicles: int = 5              # number of device_tracker slot entities

    # Optional proximity filter — if reference_stop_id is None, all vehicles
    # on the route are shown (up to max_vehicles), sorted by vehicle_label.
    reference_stop_id: str | None = None
    reference_stop_name: str | None = None
    reference_stop_lat: float | None = None
    reference_stop_lon: float | None = None
    proximity_radius_m: float = 2000.0   # ignored when reference_stop_id is None


@dataclass(slots=True)
class CoordinatorData:
    """Data returned by PTVDataUpdateCoordinator on each refresh."""

    departures: list[Departure]          # Sorted by actual_departure
    alerts: list[Alert]                  # Active alerts for this watch
