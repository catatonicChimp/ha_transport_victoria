"""Parse GTFS Realtime service alerts feed."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from ..models import Alert

_LOGGER = logging.getLogger(__name__)

# Protobuf enum → human-readable string mappings
_CAUSE_MAP = {
    0: "UNKNOWN_CAUSE",
    1: "OTHER_CAUSE",
    2: "TECHNICAL_PROBLEM",
    3: "STRIKE",
    4: "DEMONSTRATION",
    5: "ACCIDENT",
    6: "HOLIDAY",
    7: "WEATHER",
    8: "MAINTENANCE",
    9: "CONSTRUCTION",
    10: "POLICE_ACTIVITY",
    11: "MEDICAL_EMERGENCY",
}

_EFFECT_MAP = {
    0: "UNKNOWN_EFFECT",
    1: "NO_SERVICE",
    2: "REDUCED_SERVICE",
    3: "SIGNIFICANT_DELAYS",
    4: "DETOUR",
    5: "ADDITIONAL_SERVICE",
    6: "MODIFIED_SERVICE",
    7: "OTHER_EFFECT",
    8: "STOP_MOVED",
    9: "NO_EFFECT",
    10: "ACCESSIBILITY_ISSUE",
}


def parse_alerts(
    feed: object,
    stop_id: str | None = None,
    route_id: str | None = None,
) -> list[Alert]:
    """Parse a GTFS Realtime FeedMessage and return active Alert objects.

    Optionally filters to alerts that specifically mention a stop or route.
    If neither stop_id nor route_id is provided, all alerts are returned.

    Args:
        feed: A decoded gtfs_realtime_pb2.FeedMessage.
        stop_id: If provided, only return alerts that mention this stop.
        route_id: If provided, only return alerts that mention this route.

    Returns:
        List of Alert objects, newest first.
    """
    alerts: list[Alert] = []
    now_ts = datetime.now(tz=timezone.utc).timestamp()

    for entity in feed.entity:  # type: ignore[attr-defined]
        if not entity.HasField("alert"):
            continue

        raw = entity.alert

        # Filter by informed_entity if a scope is provided
        if stop_id or route_id:
            if not _entity_matches(raw, stop_id=stop_id, route_id=route_id):
                continue

        # Check whether the alert is currently active
        if raw.active_period and not _is_active(raw.active_period, now_ts):
            continue

        header = _first_translation(raw.header_text)
        description = _first_translation(raw.description_text)

        if not header:
            continue

        affected_routes = [
            ie.route_id
            for ie in raw.informed_entity
            if ie.route_id
        ]
        affected_stops = [
            ie.stop_id
            for ie in raw.informed_entity
            if ie.stop_id
        ]

        active_from = None
        active_until = None
        if raw.active_period:
            period = raw.active_period[0]
            if period.start:
                active_from = datetime.fromtimestamp(period.start, tz=timezone.utc)
            if period.end:
                active_until = datetime.fromtimestamp(period.end, tz=timezone.utc)

        alerts.append(
            Alert(
                alert_id=entity.id,
                header=header,
                description=description,
                cause=_CAUSE_MAP.get(raw.cause, "UNKNOWN_CAUSE"),
                effect=_EFFECT_MAP.get(raw.effect, "UNKNOWN_EFFECT"),
                active_from=active_from,
                active_until=active_until,
                affected_route_ids=affected_routes,
                affected_stop_ids=affected_stops,
            )
        )

    return alerts


def _entity_matches(
    alert: object,
    stop_id: str | None,
    route_id: str | None,
) -> bool:
    """Return True if the alert's informed_entity list mentions the given stop/route."""
    for ie in alert.informed_entity:  # type: ignore[attr-defined]
        if stop_id and ie.stop_id == stop_id:
            return True
        if route_id and ie.route_id == route_id:
            return True
        # An alert scoped only to a trip or agency — include it if no specific filter
        if not stop_id and not route_id:
            return True
    return False


def _is_active(active_periods: object, now_ts: float) -> bool:
    """Return True if any active period covers the current time."""
    for period in active_periods:  # type: ignore[attr-defined]
        start = period.start or 0
        end = period.end or float("inf")
        if start <= now_ts <= end:
            return True
    return False


def _first_translation(translated_string: object) -> str:
    """Return the first English (or any) translation from a TranslatedString."""
    translations = translated_string.translation  # type: ignore[attr-defined]
    if not translations:
        return ""
    # Prefer English
    for t in translations:
        if t.language in ("en", "en-AU", ""):
            return str(t.text)
    return str(translations[0].text)
