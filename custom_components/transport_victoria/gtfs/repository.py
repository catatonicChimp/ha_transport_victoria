"""GTFS schedule repository.

All database access for departure queries and stop searches lives here.
Every method is a plain synchronous function — callers must wrap them in
hass.async_add_executor_job() because SQLite access blocks the event loop.

A new sqlite3.Connection is created per call. This avoids the
"SQLite objects created in a thread can only be used in that same thread"
error that occurs when sharing a connection across the HA executor pool.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from .time_utils import (
    day_of_week_column,
    departure_seconds_to_datetime,
    service_date_str,
    service_day_for_time,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class DepartureRow:
    """A single scheduled departure, before realtime data is merged in."""

    trip_id: str
    route_id: str
    route_short_name: str
    route_long_name: str
    trip_headsign: str
    direction_id: int | None
    stop_sequence: int
    departure_seconds: int
    scheduled_departure: datetime  # absolute local datetime
    actual_stop_id: str            # specific platform stop_id that matched (for RT key)
    platform_code: str | None      # platform/track label from stops table
    route_color: str | None        # hex colour string from routes table (no '#')


@dataclass(slots=True)
class StopResult:
    """A stop returned from a name or distance search."""

    stop_id: str
    stop_name: str
    stop_lat: float | None
    stop_lon: float | None
    location_type: int
    parent_station: str | None
    distance_m: float | None = None  # only set by search_stops_by_distance


# ---------------------------------------------------------------------------
# Active services query (shared between both departure query branches)
# ---------------------------------------------------------------------------

_ACTIVE_SERVICES_SQL = """
    SELECT service_id FROM calendar
    WHERE start_date <= :service_date
      AND end_date   >= :service_date
      AND {dow_col}  = 1
    UNION
    SELECT service_id FROM calendar_dates
    WHERE date = :service_date AND exception_type = 1
    EXCEPT
    SELECT service_id FROM calendar_dates
    WHERE date = :service_date AND exception_type = 2
"""

# ---------------------------------------------------------------------------
# Departure query
# ---------------------------------------------------------------------------

_DEPARTURE_SQL = """
    SELECT
        st.trip_id,
        t.route_id,
        r.route_short_name,
        r.route_long_name,
        t.trip_headsign,
        t.direction_id,
        st.stop_sequence,
        st.departure_seconds,
        st.stop_id          AS actual_stop_id,
        s.platform_code,
        r.route_color
    FROM stop_times st
    JOIN trips t ON t.trip_id = st.trip_id
    JOIN routes r ON r.route_id = t.route_id
    JOIN stops s ON s.stop_id = st.stop_id
    WHERE st.stop_id IN (
        -- The chosen stop plus all sibling platforms sharing the same parent station,
        -- so selecting any one platform still returns all departures from that station.
        SELECT stop_id FROM stops
        WHERE stop_id = :stop_id
        UNION
        SELECT s2.stop_id FROM stops s2
        JOIN stops s1 ON s1.stop_id = :stop_id
        WHERE s2.parent_station = s1.parent_station
          AND s1.parent_station IS NOT NULL
          AND (s2.location_type = 0 OR s2.location_type IS NULL)
    )
      AND st.departure_seconds >= :now_seconds
      AND t.service_id IN ({active_services_sql}){extra_clauses}
    ORDER BY st.departure_seconds
    LIMIT :limit
"""


def next_departures(
    db_path: Path,
    stop_id: str,
    now: datetime,
    limit: int = 5,
    direction_stop_id: str | None = None,
    route_id_filter: str | None = None,
    platform_filter: str | None = None,
) -> list[DepartureRow]:
    """Return the next scheduled departures from a stop.

    Handles post-midnight trips by running two query branches:
    - Branch A: today's service date, departure_seconds >= now_seconds
    - Branch B: yesterday's service date, departure_seconds >= 86400 + now_seconds
      (catches late-night trips published under yesterday's calendar)

    Results from both branches are merged, sorted, and the first `limit` rows
    returned.

    Args:
        db_path: Path to the SQLite database.
        stop_id: The GTFS stop_id to query.
        now: Current local datetime (naive or aware).
        limit: Maximum number of departures to return.
        direction_stop_id: If set, only return trips that also serve this stop
            after the origin (used for specific-destination filtering).
        route_id_filter: If set, only return departures for this route_id.
        platform_filter: If set, only return departures from this platform_code.

    Returns:
        List of DepartureRow, sorted by departure time, length <= limit.
    """
    service_date, now_seconds = service_day_for_time(now)

    rows_a = _query_branch(
        db_path, stop_id, service_date, now_seconds, limit,
        route_id_filter=route_id_filter, platform_filter=platform_filter,
    )

    # Second branch for post-midnight: query yesterday, offset now_seconds
    yesterday = date.fromordinal(service_date.toordinal() - 1)
    rows_b = _query_branch(
        db_path, stop_id, yesterday, now_seconds + 86400, limit,
        route_id_filter=route_id_filter, platform_filter=platform_filter,
    )

    all_rows = sorted(rows_a + rows_b, key=lambda r: r.departure_seconds)

    if direction_stop_id:
        all_rows = _filter_by_destination(db_path, all_rows, direction_stop_id)

    return all_rows[:limit]


def _query_branch(
    db_path: Path,
    stop_id: str,
    service_date: date,
    now_seconds: int,
    limit: int,
    route_id_filter: str | None = None,
    platform_filter: str | None = None,
) -> list[DepartureRow]:
    """Run one branch of the departure query for a single service date."""
    dow_col = day_of_week_column(service_date)
    service_date_s = service_date_str(service_date)

    active_services_sql = _ACTIVE_SERVICES_SQL.format(dow_col=dow_col)

    # Build optional filter clauses
    extra_clauses = ""
    if route_id_filter:
        extra_clauses += "\n      AND t.route_id = :route_id_filter"
    if platform_filter:
        extra_clauses += "\n      AND s.platform_code = :platform_filter"

    sql = _DEPARTURE_SQL.format(
        active_services_sql=active_services_sql,
        extra_clauses=extra_clauses,
    )

    params: dict[str, object] = {
        "stop_id": stop_id,
        "now_seconds": now_seconds,
        "service_date": service_date_s,
        "limit": limit,
    }
    if route_id_filter:
        params["route_id_filter"] = route_id_filter
    if platform_filter:
        params["platform_filter"] = platform_filter

    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(sql, params)
        rows = []
        for row in cursor.fetchall():
            dep_secs = row["departure_seconds"]
            rows.append(
                DepartureRow(
                    trip_id=row["trip_id"],
                    route_id=row["route_id"],
                    route_short_name=row["route_short_name"] or "",
                    route_long_name=row["route_long_name"] or "",
                    trip_headsign=row["trip_headsign"] or "",
                    direction_id=row["direction_id"],
                    stop_sequence=row["stop_sequence"],
                    departure_seconds=dep_secs,
                    scheduled_departure=departure_seconds_to_datetime(
                        service_date, dep_secs
                    ),
                    actual_stop_id=row["actual_stop_id"],
                    platform_code=row["platform_code"] or None,
                    route_color=row["route_color"] or None,
                )
            )
        return rows
    except sqlite3.Error as exc:
        _LOGGER.error("Departure query failed: %s", exc)
        return []
    finally:
        conn.close()


def _filter_by_destination(
    db_path: Path,
    rows: list[DepartureRow],
    destination_stop_id: str,
) -> list[DepartureRow]:
    """Keep only trips that also call at destination_stop_id after the origin."""
    if not rows:
        return rows

    trip_ids = [r.trip_id for r in rows]
    origin_seq_by_trip = {r.trip_id: r.stop_sequence for r in rows}

    placeholders = ", ".join("?" * len(trip_ids))
    sql = f"""
        SELECT trip_id, stop_sequence FROM stop_times
        WHERE stop_id = ? AND trip_id IN ({placeholders})
    """

    conn = sqlite3.connect(str(db_path))
    try:
        qualifying: set[str] = set()
        for db_row in conn.execute(sql, [destination_stop_id] + trip_ids):
            trip_id, dest_seq = db_row
            if dest_seq > origin_seq_by_trip.get(trip_id, 0):
                qualifying.add(trip_id)
        return [r for r in rows if r.trip_id in qualifying]
    except sqlite3.Error as exc:
        _LOGGER.error("Destination filter query failed: %s", exc)
        return rows
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Stop search
# ---------------------------------------------------------------------------

def search_stops_by_name(
    db_path: Path,
    query: str,
    route_types: list[int],
    limit: int = 10,
) -> list[StopResult]:
    """Search for stops by name, filtered to stops served by the given route types.

    Only returns platform-level stops (location_type = 0 or NULL) so we don't
    surface station buildings as selectable options. Stops with the same name
    (e.g. multiple platforms of the same station) are collapsed into one result
    using the lowest stop_id as the representative.

    Args:
        db_path: Path to the SQLite database.
        query: Partial stop name to search (case-insensitive, prefix match).
        route_types: List of GTFS route_type ints to filter by.
        limit: Maximum results to return.

    Returns:
        List of StopResult sorted by name, deduplicated by name.
    """
    if not route_types:
        return []

    rt_placeholders = ", ".join("?" * len(route_types))
    sql = f"""
        SELECT MIN(s.stop_id) AS stop_id, s.stop_name,
               MIN(s.stop_lat) AS stop_lat, MIN(s.stop_lon) AS stop_lon,
               MIN(s.location_type) AS location_type,
               MIN(s.parent_station) AS parent_station
        FROM stops s
        JOIN stop_times st ON st.stop_id = s.stop_id
        JOIN trips t ON t.trip_id = st.trip_id
        JOIN routes r ON r.route_id = t.route_id
        WHERE s.stop_name LIKE ? ESCAPE '\\'
          AND (s.location_type = 0 OR s.location_type IS NULL)
          AND r.route_type IN ({rt_placeholders})
        GROUP BY lower(s.stop_name)
        ORDER BY s.stop_name COLLATE NOCASE
        LIMIT ?
    """

    like_query = _escape_like(query) + "%"
    params = [like_query] + route_types + [limit]

    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        results = []
        for row in conn.execute(sql, params):
            results.append(
                StopResult(
                    stop_id=row["stop_id"],
                    stop_name=row["stop_name"],
                    stop_lat=row["stop_lat"],
                    stop_lon=row["stop_lon"],
                    location_type=row["location_type"],
                    parent_station=row["parent_station"],
                )
            )
        return results
    except sqlite3.Error as exc:
        _LOGGER.error("Stop name search failed: %s", exc)
        return []
    finally:
        conn.close()


def search_stops_by_distance(
    db_path: Path,
    lat: float,
    lon: float,
    route_types: list[int],
    radius_m: float = 2000.0,
    limit: int = 10,
) -> list[StopResult]:
    """Find stops near a lat/lon point, sorted by distance.

    Uses a bounding-box pre-filter in SQL (fast), then computes haversine
    distance in Python to get accurate ordering and radius filtering.

    Args:
        db_path: Path to the SQLite database.
        lat: WGS84 latitude of the reference point (e.g. HA home zone).
        lon: WGS84 longitude.
        route_types: List of GTFS route_type ints to filter by.
        radius_m: Maximum distance in metres.
        limit: Maximum results to return.

    Returns:
        List of StopResult sorted by distance_m ascending.
    """
    import math

    if not route_types:
        return []

    # Degrees of latitude / longitude per metre (approximate)
    deg_per_m_lat = 1.0 / 111_320.0
    deg_per_m_lon = 1.0 / (111_320.0 * math.cos(math.radians(lat)))

    lat_margin = radius_m * deg_per_m_lat
    lon_margin = radius_m * deg_per_m_lon

    rt_placeholders = ", ".join("?" * len(route_types))
    sql = f"""
        SELECT MIN(s.stop_id) AS stop_id, s.stop_name,
               MIN(s.stop_lat) AS stop_lat, MIN(s.stop_lon) AS stop_lon,
               MIN(s.location_type) AS location_type,
               MIN(s.parent_station) AS parent_station
        FROM stops s
        JOIN stop_times st ON st.stop_id = s.stop_id
        JOIN trips t ON t.trip_id = st.trip_id
        JOIN routes r ON r.route_id = t.route_id
        WHERE (s.location_type = 0 OR s.location_type IS NULL)
          AND s.stop_lat BETWEEN ? AND ?
          AND s.stop_lon BETWEEN ? AND ?
          AND r.route_type IN ({rt_placeholders})
        GROUP BY lower(s.stop_name)
    """

    params = [
        lat - lat_margin, lat + lat_margin,
        lon - lon_margin, lon + lon_margin,
    ] + route_types

    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        candidates = []
        for row in conn.execute(sql, params):
            slat, slon = row["stop_lat"], row["stop_lon"]
            if slat is None or slon is None:
                continue
            dist = _haversine_m(lat, lon, slat, slon)
            if dist <= radius_m:
                candidates.append(
                    StopResult(
                        stop_id=row["stop_id"],
                        stop_name=row["stop_name"],
                        stop_lat=slat,
                        stop_lon=slon,
                        location_type=row["location_type"],
                        parent_station=row["parent_station"],
                        distance_m=dist,
                    )
                )

        candidates.sort(key=lambda r: r.distance_m or 0)
        return candidates[:limit]
    except sqlite3.Error as exc:
        _LOGGER.error("Stop distance search failed: %s", exc)
        return []
    finally:
        conn.close()


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return the great-circle distance in metres between two WGS84 points."""
    import math

    R = 6_371_000  # Earth radius in metres
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _escape_like(value: str) -> str:
    """Escape special characters in a LIKE pattern."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@dataclass(slots=True)
class RouteResult:
    """A route served by a stop, or from a mode-wide route search."""

    route_id: str
    route_short_name: str
    route_long_name: str
    route_color: str | None = None  # hex colour string from routes table (no '#')


def get_routes_for_stop(
    db_path: Path,
    stop_id: str,
    route_types: list[int],
) -> list[RouteResult]:
    """Return distinct routes served by a stop and its sibling platforms.

    Used in the config flow to offer a line/route filter after stop selection.
    """
    if not route_types:
        return []

    rt_placeholders = ", ".join("?" * len(route_types))
    sql = f"""
        SELECT DISTINCT r.route_id, r.route_short_name, r.route_long_name, r.route_color
        FROM stop_times st
        JOIN trips t ON t.trip_id = st.trip_id
        JOIN routes r ON r.route_id = t.route_id
        WHERE st.stop_id IN (
            SELECT stop_id FROM stops WHERE stop_id = ?
            UNION
            SELECT s2.stop_id FROM stops s2
            JOIN stops s1 ON s1.stop_id = ?
            WHERE s2.parent_station = s1.parent_station
              AND s1.parent_station IS NOT NULL
              AND (s2.location_type = 0 OR s2.location_type IS NULL)
        )
          AND r.route_type IN ({rt_placeholders})
        ORDER BY r.route_short_name COLLATE NOCASE
    """
    params: list[object] = [stop_id, stop_id] + list(route_types)

    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        results = []
        for row in conn.execute(sql, params):
            results.append(
                RouteResult(
                    route_id=row["route_id"],
                    route_short_name=row["route_short_name"] or "",
                    route_long_name=row["route_long_name"] or "",
                    route_color=row["route_color"] or None,
                )
            )
        return results
    except sqlite3.Error as exc:
        _LOGGER.error("get_routes_for_stop failed: %s", exc)
        return []
    finally:
        conn.close()


def get_platforms_for_stop(db_path: Path, stop_id: str) -> list[str]:
    """Return distinct non-null platform_codes for a stop and its siblings."""
    sql = """
        SELECT DISTINCT s.platform_code
        FROM stops s
        WHERE s.platform_code IS NOT NULL
          AND s.stop_id IN (
              SELECT stop_id FROM stops WHERE stop_id = ?
              UNION
              SELECT s2.stop_id FROM stops s2
              JOIN stops s1 ON s1.stop_id = ?
              WHERE s2.parent_station = s1.parent_station
                AND s1.parent_station IS NOT NULL
                AND (s2.location_type = 0 OR s2.location_type IS NULL)
          )
        ORDER BY s.platform_code
    """
    conn = sqlite3.connect(str(db_path))
    try:
        platforms = []
        for row in conn.execute(sql, [stop_id, stop_id]):
            platforms.append(str(row[0]))
        return platforms
    except sqlite3.Error as exc:
        _LOGGER.error("get_platforms_for_stop failed: %s", exc)
        return []
    finally:
        conn.close()


def get_service_type_baselines(
    db_path: Path,
    trip_ids: list[str],
) -> dict[str, tuple[int, int]]:
    """Return {trip_id: (trip_stop_count, baseline_max)} for service-type classification.

    The baseline is the maximum stop count among all trips that share the same
    (route_id, direction_id, terminal_stop_id) group.  This prevents short-working
    trips (e.g. a Coburg-terminating train) from being incorrectly classified as
    express simply because a longer Craigieburn service exists on the same route.
    """
    if not trip_ids:
        return {}
    tp = ", ".join("?" * len(trip_ids))
    sql = f"""
        WITH
        -- Route/direction for each target trip
        target AS (
            SELECT trip_id, route_id, direction_id
            FROM trips
            WHERE trip_id IN ({tp})
        ),
        -- Stop count and last sequence number for each target trip
        target_stats AS (
            SELECT trip_id, COUNT(*) AS stop_count, MAX(stop_sequence) AS max_seq
            FROM stop_times
            WHERE trip_id IN ({tp})
            GROUP BY trip_id
        ),
        -- Terminal stop (last stop in sequence) for each target trip
        target_terminal AS (
            SELECT st.trip_id, st.stop_id AS terminal_stop_id
            FROM stop_times st
            JOIN target_stats ts ON ts.trip_id = st.trip_id
                                 AND st.stop_sequence = ts.max_seq
        ),
        -- All trips on the same routes (to build group baselines)
        route_trips AS (
            SELECT t.trip_id, t.route_id, t.direction_id
            FROM trips t
            WHERE t.route_id IN (SELECT DISTINCT route_id FROM target)
        ),
        -- Stop count + terminal for every trip on those routes
        route_trip_stats AS (
            SELECT trip_id, COUNT(*) AS stop_count, MAX(stop_sequence) AS max_seq
            FROM stop_times
            WHERE trip_id IN (SELECT trip_id FROM route_trips)
            GROUP BY trip_id
        ),
        route_trip_terminal AS (
            SELECT st.trip_id, st.stop_id AS terminal_stop_id
            FROM stop_times st
            JOIN route_trip_stats rts ON rts.trip_id = st.trip_id
                                      AND st.stop_sequence = rts.max_seq
        ),
        -- Maximum stop count per (route, direction, terminal) group
        group_baseline AS (
            SELECT rt.route_id, rt.direction_id, rtt.terminal_stop_id,
                   MAX(rts.stop_count) AS max_count
            FROM route_trips rt
            JOIN route_trip_stats rts ON rts.trip_id = rt.trip_id
            JOIN route_trip_terminal rtt ON rtt.trip_id = rt.trip_id
            GROUP BY rt.route_id, rt.direction_id, rtt.terminal_stop_id
        )
        SELECT tgt.trip_id, ts.stop_count, gb.max_count
        FROM target tgt
        JOIN target_stats ts ON ts.trip_id = tgt.trip_id
        JOIN target_terminal tt ON tt.trip_id = tgt.trip_id
        JOIN group_baseline gb ON gb.route_id = tgt.route_id
                               AND gb.direction_id = tgt.direction_id
                               AND gb.terminal_stop_id = tt.terminal_stop_id
    """
    params = trip_ids + trip_ids
    conn = sqlite3.connect(str(db_path))
    try:
        result: dict[str, tuple[int, int]] = {}
        for row in conn.execute(sql, params):
            result[row[0]] = (row[1], row[2])
        return result
    except sqlite3.Error as exc:
        _LOGGER.error("get_service_type_baselines failed: %s", exc)
        return {}
    finally:
        conn.close()


def get_arrival_at_stop(
    db_path: Path,
    trip_id: str,
    stop_id: str,
    service_date: date,
) -> datetime | None:
    """Return the scheduled arrival datetime at a stop for a given trip.

    Used to compute transfer windows in journey mode.
    """
    sql = """
        SELECT arrival_seconds FROM stop_times
        WHERE trip_id = ? AND stop_id = ?
        LIMIT 1
    """
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(sql, [trip_id, stop_id]).fetchone()
        if row is None:
            return None
        return departure_seconds_to_datetime(service_date, row[0])
    except sqlite3.Error as exc:
        _LOGGER.error("get_arrival_at_stop failed: %s", exc)
        return None
    finally:
        conn.close()


def is_db_ready(db_path: Path) -> bool:
    """Return True if the database exists and has data in the stops table."""
    if not db_path.exists():
        return False
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute("SELECT COUNT(*) FROM stops").fetchone()
            return (row[0] or 0) > 0
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def is_mode_ready(db_path: Path, bundle_folders: list[str]) -> bool:
    """Return True if all requested bundle folders have been imported."""
    if not db_path.exists():
        return False
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            placeholders = ", ".join("?" * len(bundle_folders))
            keys = [f"bundle_{f}" for f in bundle_folders]
            row = conn.execute(
                f"SELECT COUNT(*) FROM import_meta WHERE key IN ({placeholders})",
                keys,
            ).fetchone()
            return (row[0] or 0) >= len(bundle_folders)
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def get_all_routes(
    db_path: Path,
    route_types: list[int],
    limit: int = 100,
) -> list[RouteResult]:
    """Return all routes for the given route types, ordered by route_short_name.

    Used by the route tracker config flow to populate the initial route picker.
    """
    if not route_types:
        return []

    rt_placeholders = ", ".join("?" * len(route_types))
    sql = f"""
        SELECT DISTINCT r.route_id, r.route_short_name, r.route_long_name, r.route_color
        FROM routes r
        WHERE r.route_type IN ({rt_placeholders})
          AND (r.route_long_name NOT LIKE 'Replacement%' OR r.route_long_name IS NULL)
          AND (r.route_short_name NOT LIKE 'Replacement%' OR r.route_short_name IS NULL)
        ORDER BY
            CASE WHEN CAST(r.route_short_name AS INTEGER) > 0
                 THEN CAST(r.route_short_name AS INTEGER)
                 ELSE 9999999 END,
            r.route_short_name COLLATE NOCASE
        LIMIT ?
    """
    params: list[int | str] = [*route_types, limit]

    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        results = []
        for row in conn.execute(sql, params):
            results.append(
                RouteResult(
                    route_id=row["route_id"],
                    route_short_name=row["route_short_name"] or "",
                    route_long_name=row["route_long_name"] or "",
                    route_color=row["route_color"] or None,
                )
            )
        return results
    except sqlite3.Error as exc:
        _LOGGER.error("get_all_routes failed: %s", exc)
        return []
    finally:
        conn.close()


def search_routes_by_name(
    db_path: Path,
    query: str,
    route_types: list[int],
    limit: int = 20,
) -> list[RouteResult]:
    """Search routes by short or long name (case-insensitive prefix match).

    Used by the route tracker config flow search box.
    """
    if not route_types:
        return []

    rt_placeholders = ", ".join("?" * len(route_types))
    like_query = _escape_like(query) + "%"
    sql = f"""
        SELECT DISTINCT r.route_id, r.route_short_name, r.route_long_name, r.route_color
        FROM routes r
        WHERE (r.route_short_name LIKE ? ESCAPE '\\'
            OR r.route_long_name LIKE ? ESCAPE '\\')
          AND r.route_type IN ({rt_placeholders})
          AND (r.route_long_name NOT LIKE 'Replacement%' OR r.route_long_name IS NULL)
          AND (r.route_short_name NOT LIKE 'Replacement%' OR r.route_short_name IS NULL)
        ORDER BY
            CASE WHEN CAST(r.route_short_name AS INTEGER) > 0
                 THEN CAST(r.route_short_name AS INTEGER)
                 ELSE 9999999 END,
            r.route_short_name COLLATE NOCASE
        LIMIT ?
    """
    params: list[int | str] = [like_query, like_query, *route_types, limit]

    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        results = []
        for row in conn.execute(sql, params):
            results.append(
                RouteResult(
                    route_id=row["route_id"],
                    route_short_name=row["route_short_name"] or "",
                    route_long_name=row["route_long_name"] or "",
                    route_color=row["route_color"] or None,
                )
            )
        return results
    except sqlite3.Error as exc:
        _LOGGER.error("search_routes_by_name failed: %s", exc)
        return []
    finally:
        conn.close()


def get_stop_lat_lon(
    db_path: Path,
    stop_id: str,
) -> tuple[float, float] | None:
    """Return the (lat, lon) of a stop, or None if not found.

    Used during route tracker config flow to store the reference stop's
    coordinates in the config entry (avoids a DB lookup on every poll).
    """
    sql = "SELECT stop_lat, stop_lon FROM stops WHERE stop_id = ? LIMIT 1"
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(sql, [stop_id]).fetchone()
        if row is None or row[0] is None or row[1] is None:
            return None
        return (float(row[0]), float(row[1]))
    except sqlite3.Error as exc:
        _LOGGER.error("get_stop_lat_lon failed: %s", exc)
        return None
    finally:
        conn.close()
