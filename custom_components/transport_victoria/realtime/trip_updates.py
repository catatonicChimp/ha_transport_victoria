"""Parse GTFS Realtime trip-update feeds into per-stop delay maps.

The trip-updates feed carries delay and cancellation information keyed by
trip_id + stop_id. This module converts raw FeedMessage objects into a simple
dict so the coordinator can merge realtime data into scheduled departures
without touching protobuf objects outside this module.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

_LOGGER = logging.getLogger(__name__)

# ScheduleRelationship enum values from the GTFS Realtime spec
_SR_CANCELED = 3


@dataclass(slots=True, frozen=True)
class StopDelay:
    """Realtime delay information for one trip at one stop."""

    delay_seconds: int
    is_cancelled: bool


def parse_trip_updates(feed: object) -> dict[str, StopDelay]:
    """Parse a TripUpdates FeedMessage into a delay lookup dict.

    The returned dict is keyed by ``"<trip_id>:<stop_id>"`` so the coordinator
    can do an O(1) lookup for each scheduled departure.

    When a stop_time_update carries both arrival and departure delays, the
    departure delay is preferred (it's what passengers care about). If only
    arrival is present, that value is used instead.

    Args:
        feed: A decoded ``gtfs_realtime_pb2.FeedMessage`` instance.

    Returns:
        Dict mapping "trip_id:stop_id" → StopDelay.
    """
    result: dict[str, StopDelay] = {}

    try:
        entities = feed.entity  # type: ignore[attr-defined]
    except AttributeError:
        _LOGGER.warning("parse_trip_updates received unexpected feed type: %s", type(feed))
        return result

    for entity in entities:
        if not entity.HasField("trip_update"):
            continue

        trip_update = entity.trip_update
        trip_id: str = trip_update.trip.trip_id

        if not trip_id:
            continue

        is_cancelled = (
            trip_update.trip.schedule_relationship == _SR_CANCELED
        )

        for stu in trip_update.stop_time_update:
            stop_id: str = stu.stop_id
            if not stop_id:
                continue

            delay = 0
            if stu.HasField("departure") and stu.departure.HasField("delay"):
                delay = stu.departure.delay
            elif stu.HasField("arrival") and stu.arrival.HasField("delay"):
                delay = stu.arrival.delay

            key = f"{trip_id}:{stop_id}"
            result[key] = StopDelay(delay_seconds=delay, is_cancelled=is_cancelled)

    _LOGGER.debug("Parsed %d stop-level trip updates", len(result))
    return result
