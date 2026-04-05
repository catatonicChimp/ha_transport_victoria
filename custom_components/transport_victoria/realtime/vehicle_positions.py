"""GTFS Realtime vehicle-positions parser.

Converts a FeedMessage protobuf into a flat dict keyed by trip_id for O(1)
lookup during vehicle-tracker coordinator updates.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

_LOGGER = logging.getLogger(__name__)

# Numeric → string mapping for VehicleStopStatus
_STATUS_MAP: dict[int, str] = {
    0: "INCOMING_AT",
    1: "STOPPED_AT",
    2: "IN_TRANSIT_TO",
}


@dataclass(slots=True)
class VehiclePosition:
    """The last-known GPS position of a vehicle serving a trip."""

    trip_id: str
    latitude: float
    longitude: float
    bearing: float | None
    speed: float | None       # metres per second from the feed
    speed_kmh: float | None   # converted for display
    timestamp: datetime | None
    vehicle_id: str | None
    vehicle_label: str | None
    current_stop_id: str | None      # stop the vehicle is at / heading to
    current_status: str | None       # INCOMING_AT / STOPPED_AT / IN_TRANSIT_TO
    route_id: str | None = None      # from trip descriptor in the realtime feed


def parse_vehicle_positions(feed: object) -> dict[str, VehiclePosition]:
    """Parse a GTFS Realtime FeedMessage and return positions keyed by trip_id.

    Args:
        feed: A decoded gtfs_realtime_pb2.FeedMessage instance.

    Returns:
        dict mapping trip_id → VehiclePosition.  Entries without a valid
        latitude/longitude or trip_id are silently dropped.
    """
    result: dict[str, VehiclePosition] = {}

    for entity in feed.entity:  # type: ignore[attr-defined]
        if not entity.HasField("vehicle"):
            continue
        veh = entity.vehicle

        # trip_id is required; skip if missing
        trip_id = veh.trip.trip_id if veh.HasField("trip") else ""
        if not trip_id:
            continue
        route_id: str | None = (veh.trip.route_id or None) if veh.HasField("trip") else None

        # Position
        if not veh.HasField("position"):
            continue
        pos = veh.position
        lat: float = pos.latitude
        lon: float = pos.longitude
        if lat == 0.0 and lon == 0.0:
            # Treat 0,0 as no data (protobuf default)
            continue

        bearing: float | None = float(pos.bearing) if pos.bearing else None
        speed_mps: float | None = float(pos.speed) if pos.speed else None
        speed_kmh: float | None = round(speed_mps * 3.6, 1) if speed_mps is not None else None

        # Timestamp
        ts: datetime | None = None
        if veh.timestamp:
            ts = datetime.fromtimestamp(veh.timestamp, tz=timezone.utc)

        # Vehicle identity
        vehicle_id: str | None = veh.vehicle.id if veh.vehicle.id else None
        vehicle_label: str | None = veh.vehicle.label if veh.vehicle.label else None

        # Current stop/status
        current_stop_id: str | None = veh.stop_id if veh.stop_id else None
        current_status: str | None = _STATUS_MAP.get(veh.current_status)

        result[trip_id] = VehiclePosition(
            trip_id=trip_id,
            latitude=lat,
            longitude=lon,
            bearing=bearing,
            speed=speed_mps,
            speed_kmh=speed_kmh,
            timestamp=ts,
            vehicle_id=vehicle_id,
            vehicle_label=vehicle_label,
            current_stop_id=current_stop_id,
            current_status=current_status,
            route_id=route_id,
        )

    _LOGGER.debug("Parsed %d vehicle positions", len(result))
    return result
