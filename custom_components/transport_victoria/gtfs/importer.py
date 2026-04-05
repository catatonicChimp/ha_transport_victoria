"""GTFS ZIP importer.

Reads a PTV GTFS ZIP file and imports all tables into a SQLite database.
Uses stdlib csv + sqlite3.executemany — no pandas, no ORM — so it runs
quickly on low-power hardware and adds no extra HA dependencies.

Import is done into a temporary database file, then atomically swapped into
place with os.replace() so in-flight queries against the old DB are unaffected.

Typical import time for the ~229 MB PTV ZIP: under 2 minutes on a Pi 4.

Optimisations applied:
  - shapes.txt is skipped (11 M geometry rows with no value for departure queries)
  - stop_times.txt is filtered to trips active within the next 45 days, reducing
    the bus bundle from ~18 M rows to a much smaller working set
  - calendar / calendar_dates / trips are imported before stop_times so the
    valid-trip filter can be computed from data already in the database
"""

from __future__ import annotations

import csv
import io
import logging
import os
import sqlite3
import zipfile
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .time_utils import gtfs_time_to_seconds

_LOGGER = logging.getLogger(__name__)

# Chunk size for executemany batches — keeps memory use bounded
_BATCH_SIZE = 5000

# Only import stop_times for trips that run within this many days from today
_TRIP_WINDOW_DAYS = 45


def _optional_int(value: str) -> int | None:
    return int(value) if value.strip() else None


def _optional_float(value: str) -> float | None:
    return float(value) if value.strip() else None


# GTFS files we import and their target tables, in processing order.
# calendar / calendar_dates / trips must come before stop_times so that
# _compute_valid_trip_ids() has data to query when stop_times is processed.
# shapes.txt is intentionally excluded — see module docstring.
_GTFS_FILES: dict[str, str] = {
    "agency.txt": "agency",
    "routes.txt": "routes",
    "calendar.txt": "calendar",
    "calendar_dates.txt": "calendar_dates",
    "trips.txt": "trips",
    "stops.txt": "stops",
    "stop_times.txt": "stop_times",
}

# Column definitions per table: (csv_column, db_column, type_converter)
# type_converter is called on the raw string value. None means pass through as-is.
# departure_seconds / arrival_seconds are computed from the time string columns.
_TABLE_COLUMNS: dict[str, list[tuple[str, str, Callable[[str], Any] | None]]] = {
    "agency": [
        ("agency_id", "agency_id", None),
        ("agency_name", "agency_name", None),
        ("agency_url", "agency_url", None),
        ("agency_timezone", "agency_timezone", None),
        ("agency_lang", "agency_lang", None),
        ("agency_phone", "agency_phone", None),
    ],
    "routes": [
        ("route_id", "route_id", None),
        ("agency_id", "agency_id", None),
        ("route_short_name", "route_short_name", None),
        ("route_long_name", "route_long_name", None),
        ("route_type", "route_type", int),
        ("route_color", "route_color", None),
        ("route_text_color", "route_text_color", None),
    ],
    "trips": [
        ("trip_id", "trip_id", None),
        ("route_id", "route_id", None),
        ("service_id", "service_id", None),
        ("shape_id", "shape_id", None),
        ("trip_headsign", "trip_headsign", None),
        ("direction_id", "direction_id", _optional_int),
        ("block_id", "block_id", None),
    ],
    "stops": [
        ("stop_id", "stop_id", None),
        ("stop_name", "stop_name", None),
        ("stop_lat", "stop_lat", _optional_float),
        ("stop_lon", "stop_lon", _optional_float),
        ("stop_code", "stop_code", None),
        ("location_type", "location_type", _optional_int),
        ("parent_station", "parent_station", None),
        ("platform_code", "platform_code", None),
        ("wheelchair_boarding", "wheelchair_boarding", _optional_int),
    ],
    # stop_times has special handling — see _import_stop_times()
    "stop_times": [],
    "calendar": [
        ("service_id", "service_id", None),
        ("monday", "monday", int),
        ("tuesday", "tuesday", int),
        ("wednesday", "wednesday", int),
        ("thursday", "thursday", int),
        ("friday", "friday", int),
        ("saturday", "saturday", int),
        ("sunday", "sunday", int),
        ("start_date", "start_date", None),
        ("end_date", "end_date", None),
    ],
    "calendar_dates": [
        ("service_id", "service_id", None),
        ("date", "date", None),
        ("exception_type", "exception_type", int),
    ],
}


def _compute_valid_trip_ids(
    conn: sqlite3.Connection,
    window_days: int = _TRIP_WINDOW_DAYS,
) -> frozenset[str]:
    """Return trip_ids whose service runs within the next window_days days.

    Queries calendar and calendar_dates from the already-imported data in conn.
    Errs on the side of inclusion: a service is included if it has any active
    dates in the window (individual exception removals are not subtracted here).
    This keeps the logic simple while still cutting the majority of stale rows.
    """
    today = date.today()
    end = today + timedelta(days=window_days)
    today_str = today.strftime("%Y%m%d")
    end_str = end.strftime("%Y%m%d")

    sql = """
        SELECT DISTINCT t.trip_id
        FROM trips t
        WHERE t.service_id IN (
            -- Regular calendar services whose date range overlaps the window
            -- and has at least one scheduled day of the week
            SELECT service_id FROM calendar
             WHERE start_date <= ? AND end_date >= ?
               AND (monday=1 OR tuesday=1 OR wednesday=1 OR thursday=1
                    OR friday=1 OR saturday=1 OR sunday=1)
            UNION
            -- Exception-added service dates within the window
            SELECT service_id FROM calendar_dates
             WHERE date >= ? AND date <= ? AND exception_type = 1
        )
    """
    try:
        rows = conn.execute(sql, [end_str, today_str, today_str, end_str]).fetchall()
        return frozenset(row[0] for row in rows)
    except sqlite3.Error as exc:
        _LOGGER.warning(
            "Could not compute valid trip IDs (calendar not yet imported?): %s — "
            "importing all stop_times without filter",
            exc,
        )
        return frozenset()


def _import_gtfs_files(
    conn: sqlite3.Connection,
    zf: zipfile.ZipFile,
    bundle_label: str,
) -> None:
    """Import all known GTFS txt files from an open ZipFile into conn.

    calendar / calendar_dates / trips are processed before stop_times (see
    _GTFS_FILES ordering) so that _compute_valid_trip_ids() has data to query
    when stop_times is reached.
    """
    files = list(_GTFS_FILES.items())
    prefix = f"[{bundle_label}] " if bundle_label else ""
    trip_id_filter: frozenset[str] | None = None

    for idx, (filename, table) in enumerate(files, start=1):
        reader = _open_csv(zf, filename)
        if reader is None:
            continue

        _LOGGER.info("%sImporting %s [%d/%d]", prefix, filename, idx, len(files))
        t0 = datetime.now(tz=timezone.utc)

        if table == "stop_times":
            if trip_id_filter is None:
                trip_id_filter = _compute_valid_trip_ids(conn)
                _LOGGER.info(
                    "%sTrip filter: %d trips active in next %d days",
                    prefix,
                    len(trip_id_filter),
                    _TRIP_WINDOW_DAYS,
                )
            count = _import_stop_times(conn, reader, trip_id_filter)
        else:
            count = _import_generic(conn, reader, table, _TABLE_COLUMNS[table])

        elapsed_s = (datetime.now(tz=timezone.utc) - t0).total_seconds()
        _LOGGER.info(
            "%s%s done — %d rows in %.1fs", prefix, table, count, elapsed_s
        )
        conn.commit()


def _load_schema(conn: sqlite3.Connection) -> None:
    """Create tables and indexes from schema.sql."""
    schema_path = Path(__file__).parent / "schema.sql"
    conn.executescript(schema_path.read_text(encoding="utf-8"))


def _open_csv(zf: zipfile.ZipFile, filename: str) -> csv.DictReader[str] | None:
    """Open a file from a ZIP as a DictReader, handling UTF-8 BOM."""
    try:
        raw = zf.open(filename)
    except KeyError:
        _LOGGER.debug("GTFS file not present in ZIP: %s", filename)
        return None
    return csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig"))


def _import_generic(
    conn: sqlite3.Connection,
    reader: csv.DictReader[str],
    table: str,
    col_defs: list[tuple[str, str, Callable[[str], Any] | None]],
) -> int:
    """Import rows from a DictReader into a table using batched executemany."""
    db_cols = [col[1] for col in col_defs]
    placeholders = ", ".join("?" * len(db_cols))
    sql = f"INSERT OR REPLACE INTO {table} ({', '.join(db_cols)}) VALUES ({placeholders})"

    batch: list[tuple[Any, ...]] = []
    total = 0

    for row in reader:
        values: list[Any] = []
        for csv_col, _db_col, converter in col_defs:
            raw = row.get(csv_col, "") or ""
            values.append(converter(raw) if converter and raw.strip() else (raw or None))
        batch.append(tuple(values))

        if len(batch) >= _BATCH_SIZE:
            conn.executemany(sql, batch)
            total += len(batch)
            batch = []

    if batch:
        conn.executemany(sql, batch)
        total += len(batch)

    return total


def _import_stop_times(
    conn: sqlite3.Connection,
    reader: csv.DictReader[str],
    trip_id_filter: frozenset[str] | None = None,
) -> int:
    """Import stop_times with computed arrival_seconds and departure_seconds.

    stop_times is the largest file. We convert HH:MM:SS time strings to
    integer seconds during import so that departure queries are fast indexed
    integer comparisons rather than string operations.

    If trip_id_filter is provided, only rows whose trip_id is in the set are
    imported. This is used to skip trips that don't run in the next 45 days,
    significantly reducing DB size for the bus bundles.
    """
    sql = """
        INSERT OR REPLACE INTO stop_times
            (trip_id, stop_id, stop_sequence,
             arrival_seconds, departure_seconds,
             pickup_type, drop_off_type, timepoint)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """

    batch: list[tuple[Any, ...]] = []
    total = 0
    skipped = 0

    for row in reader:
        trip_id = row.get("trip_id") or None

        # Filter to active trips when a filter set is provided
        if trip_id_filter is not None and trip_id not in trip_id_filter:
            skipped += 1
            continue

        arrival_raw = row.get("arrival_time", "").strip()
        departure_raw = row.get("departure_time", "").strip()

        try:
            arrival_seconds = gtfs_time_to_seconds(arrival_raw) if arrival_raw else None
        except ValueError:
            arrival_seconds = None

        try:
            departure_seconds = gtfs_time_to_seconds(departure_raw) if departure_raw else None
        except ValueError:
            departure_seconds = None

        batch.append((
            trip_id,
            row.get("stop_id") or None,
            int(row.get("stop_sequence", 0) or 0),
            arrival_seconds,
            departure_seconds,
            int(row.get("pickup_type", 0) or 0),
            int(row.get("drop_off_type", 0) or 0),
            int(row.get("timepoint", 1) or 1),
        ))

        if len(batch) >= _BATCH_SIZE:
            conn.executemany(sql, batch)
            total += len(batch)
            batch = []

    if batch:
        conn.executemany(sql, batch)
        total += len(batch)

    if skipped:
        _LOGGER.debug("stop_times: skipped %d rows (trip outside 45-day window)", skipped)

    return total


def import_mode_bundles(
    zip_path: Path,
    db_path: Path,
    zip_sha256: str,
    bundle_folders: list[str],
    *,
    force: bool = False,
    zip_etag: str | None = None,
    zip_size: str | None = None,
) -> None:
    """Import specific mode bundle folders from the PTV outer ZIP.

    Only the requested folders (e.g. ["2"] for metro trains) are imported.
    Bundles already recorded in import_meta are skipped unless force=True.

    When force=True (ZIP content has changed), all bundles are re-imported
    into a fresh temporary database that is atomically swapped into place.
    This ensures in-flight queries against the old DB are unaffected.

    Args:
        zip_path: Path to the downloaded outer GTFS ZIP.
        db_path:  Destination SQLite database path.
        zip_sha256: SHA-256 of the outer ZIP, stored in import_meta.
        bundle_folders: Folder numbers to import, e.g. ["2", "5"].
        force: If True, re-import all bundles even if already recorded.
               Use when the ZIP content has changed (new weekly data).
        zip_etag: ETag header from the download, stored for future HEAD checks.
        zip_size: Content-Length header from the download, stored as fallback.
    """
    if force:
        to_import = list(bundle_folders)
    else:
        already_imported = get_imported_bundles(db_path)
        to_import = [f for f in bundle_folders if f not in already_imported]

    if not to_import:
        _LOGGER.debug("All requested bundles already imported: %s", bundle_folders)
        return

    # Use a temp file when: DB doesn't exist yet, or force (full re-import from new ZIP)
    use_temp = not db_path.exists() or force
    target_path = db_path.with_suffix(".importing.db") if use_temp else db_path

    if use_temp and target_path.exists():
        target_path.unlink()

    _LOGGER.info(
        "Starting GTFS import: bundles %s -> %s (force=%s)", to_import, target_path.name, force
    )
    start = datetime.now(tz=timezone.utc)

    conn = sqlite3.connect(str(target_path))
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA cache_size = -64000")

        _load_schema(conn)  # CREATE TABLE IF NOT EXISTS — safe to call on existing DB

        with zipfile.ZipFile(zip_path, "r") as outer_zf:
            all_inner = {
                n.split("/")[0]: n
                for n in outer_zf.namelist()
                if n.endswith("/google_transit.zip")
            }

            if all_inner:
                for idx, folder in enumerate(to_import, start=1):
                    inner_name = all_inner.get(folder)
                    if inner_name is None:
                        _LOGGER.warning("Bundle folder %s not found in ZIP", folder)
                        continue
                    _LOGGER.info(
                        "Importing bundle %d/%d: folder %s", idx, len(to_import), folder
                    )
                    inner_zf = zipfile.ZipFile(io.BytesIO(outer_zf.read(inner_name)))
                    _import_gtfs_files(conn, inner_zf, bundle_label=folder)
            else:
                # Flat GTFS — treat the whole ZIP as one bundle
                _LOGGER.info("Flat GTFS format detected — importing all files")
                _import_gtfs_files(conn, outer_zf, bundle_label="")

        # Record which bundles are now imported, plus download metadata
        imported_at = datetime.now(tz=timezone.utc).isoformat()
        meta_rows: list[tuple[str, str]] = [
            ("zip_sha256", zip_sha256),
            ("imported_at", imported_at),
        ]
        if zip_etag:
            meta_rows.append(("zip_etag", zip_etag))
        if zip_size:
            meta_rows.append(("zip_size", zip_size))
        for folder in to_import:
            meta_rows.append((f"bundle_{folder}", imported_at))

        conn.executemany(
            "INSERT OR REPLACE INTO import_meta (key, value) VALUES (?, ?)", meta_rows
        )
        conn.commit()

    except Exception:
        conn.close()
        if use_temp and target_path.exists():
            target_path.unlink()
        raise
    else:
        conn.close()

    if use_temp:
        os.replace(target_path, db_path)

    elapsed = (datetime.now(tz=timezone.utc) - start).total_seconds()
    _LOGGER.info(
        "GTFS import complete: bundles %s in %.1fs", to_import, elapsed
    )


def get_imported_bundles(db_path: Path) -> set[str]:
    """Return the set of bundle folder numbers already imported into db_path.

    Returns an empty set if the database does not exist or has no import_meta.
    """
    if not db_path.exists():
        return set()
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                "SELECT key FROM import_meta WHERE key LIKE 'bundle_%'"
            ).fetchall()
            return {row[0].removeprefix("bundle_") for row in rows}
        finally:
            conn.close()
    except sqlite3.Error:
        return set()


def get_stored_zip_meta(db_path: Path) -> dict[str, str]:
    """Return stored ZIP metadata from import_meta.

    Returns a dict with any of the following keys present:
      'zip_sha256', 'zip_etag', 'zip_size'

    Returns an empty dict if the database does not exist or has no records.
    """
    if not db_path.exists():
        return {}
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                "SELECT key, value FROM import_meta"
                " WHERE key IN ('zip_sha256', 'zip_etag', 'zip_size')"
            ).fetchall()
            return {row[0]: row[1] for row in rows}
        finally:
            conn.close()
    except sqlite3.Error:
        return {}


# ---------------------------------------------------------------------------
# Legacy helper — kept for backward compat; delegates to get_stored_zip_meta
# ---------------------------------------------------------------------------

def get_stored_sha256(db_path: Path) -> str | None:
    """Return the SHA-256 of the last successfully imported ZIP, or None."""
    return get_stored_zip_meta(db_path).get("zip_sha256")


# ---------------------------------------------------------------------------
# Legacy helper — imports all bundles; kept for CLI/test use
# ---------------------------------------------------------------------------

def import_zip(zip_path: Path, db_path: Path, zip_sha256: str) -> None:
    """Import all bundles from the ZIP (used by weekly refresh for all modes)."""
    with zipfile.ZipFile(zip_path, "r") as outer_zf:
        all_folders = sorted(
            n.split("/")[0]
            for n in outer_zf.namelist()
            if n.endswith("/google_transit.zip")
        )
    folders = all_folders if all_folders else [""]
    import_mode_bundles(zip_path, db_path, zip_sha256, folders)
