"""Unit tests for gtfs/importer.py."""

from __future__ import annotations

import csv
import io
import sqlite3
import zipfile
from datetime import date, timedelta
from pathlib import Path

import pytest

from custom_components.transport_victoria.gtfs.importer import (
    _compute_valid_trip_ids,
    _import_stop_times,
    get_imported_bundles,
    get_stored_zip_meta,
    import_mode_bundles,
)

_SCHEMA_PATH = Path(__file__).parents[3] / "custom_components" / "transport_victoria" / "gtfs" / "schema.sql"


def _make_db(path: Path) -> sqlite3.Connection:
    """Create a fresh DB from schema.sql and return an open connection."""
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
    return conn


def _make_gtfs_zip(tmp_path: Path, folder: str = "2") -> Path:
    """Build a minimal nested PTV GTFS ZIP at tmp_path/gtfs.zip."""
    today = date.today()
    yesterday = (today - timedelta(days=1)).strftime("%Y%m%d")
    tomorrow = (today + timedelta(days=30)).strftime("%Y%m%d")
    dow = today.strftime("%A").lower()

    cal_row = {"service_id": "SVC1", "start_date": yesterday, "end_date": tomorrow,
               "monday": "0", "tuesday": "0", "wednesday": "0", "thursday": "0",
               "friday": "0", "saturday": "0", "sunday": "0"}
    cal_row[dow] = "1"

    def _csv(rows: list[dict]) -> bytes:
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
        return buf.getvalue().encode("utf-8")

    now_secs = 9 * 3600  # 09:00

    inner_buf = io.BytesIO()
    with zipfile.ZipFile(inner_buf, "w") as inner:
        inner.writestr("agency.txt", _csv([
            {"agency_id": "1", "agency_name": "Metro", "agency_url": "",
             "agency_timezone": "Australia/Melbourne", "agency_lang": "en", "agency_phone": ""},
        ]))
        inner.writestr("routes.txt", _csv([
            {"route_id": "R1", "agency_id": "1", "route_short_name": "Craigieburn",
             "route_long_name": "Craigieburn Line", "route_type": "400",
             "route_color": "0099CC", "route_text_color": "FFFFFF"},
        ]))
        inner.writestr("calendar.txt", _csv([cal_row]))
        inner.writestr("calendar_dates.txt", _csv([
            {"service_id": "SVC1", "date": today.strftime("%Y%m%d"), "exception_type": "1"},
        ]))
        inner.writestr("trips.txt", _csv([
            {"trip_id": "T1", "route_id": "R1", "service_id": "SVC1",
             "shape_id": "", "trip_headsign": "Flinders Street", "direction_id": "0", "block_id": ""},
        ]))
        inner.writestr("stops.txt", _csv([
            {"stop_id": "S1", "stop_name": "Test Station", "stop_lat": "-37.74",
             "stop_lon": "144.96", "stop_code": "1", "location_type": "",
             "parent_station": "", "platform_code": "", "wheelchair_boarding": ""},
        ]))
        inner.writestr("stop_times.txt", _csv([
            {"trip_id": "T1", "stop_id": "S1", "stop_sequence": "1",
             "arrival_time": "09:00:00", "departure_time": "09:00:00",
             "pickup_type": "0", "drop_off_type": "0", "timepoint": "1"},
        ]))
    inner_buf.seek(0)

    outer_path = tmp_path / "gtfs.zip"
    with zipfile.ZipFile(outer_path, "w") as outer:
        outer.writestr(f"{folder}/google_transit.zip", inner_buf.getvalue())
    return outer_path


# ---------------------------------------------------------------------------
# _compute_valid_trip_ids
# ---------------------------------------------------------------------------

class TestComputeValidTripIds:
    def test_includes_active_trips(self, tmp_path: Path):
        conn = _make_db(tmp_path / "db.db")
        today = date.today()
        dow = today.strftime("%A").lower()
        cal = {"monday": 0, "tuesday": 0, "wednesday": 0, "thursday": 0,
               "friday": 0, "saturday": 0, "sunday": 0}
        cal[dow] = 1
        conn.execute(
            "INSERT INTO calendar VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("SVC1", *list(cal.values()),
             (today - timedelta(1)).strftime("%Y%m%d"),
             (today + timedelta(30)).strftime("%Y%m%d")),
        )
        conn.execute("INSERT INTO trips VALUES (?, ?, ?, ?, ?, ?, ?)",
                     ("T1", "R1", "SVC1", None, "Test", 0, None))
        conn.commit()
        result = _compute_valid_trip_ids(conn, window_days=45)
        assert "T1" in result
        conn.close()

    def test_excludes_expired_trips(self, tmp_path: Path):
        conn = _make_db(tmp_path / "db.db")
        past = (date.today() - timedelta(100)).strftime("%Y%m%d")
        past_end = (date.today() - timedelta(60)).strftime("%Y%m%d")
        conn.execute(
            "INSERT INTO calendar VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("SVC_OLD", 1, 1, 1, 1, 1, 1, 1, past, past_end),
        )
        conn.execute("INSERT INTO trips VALUES (?, ?, ?, ?, ?, ?, ?)",
                     ("T_OLD", "R1", "SVC_OLD", None, "Old", 0, None))
        conn.commit()
        result = _compute_valid_trip_ids(conn, window_days=45)
        assert "T_OLD" not in result
        conn.close()

    def test_empty_calendar_returns_empty(self, tmp_path: Path):
        conn = _make_db(tmp_path / "db.db")
        result = _compute_valid_trip_ids(conn)
        assert result == frozenset()
        conn.close()


# ---------------------------------------------------------------------------
# import_mode_bundles
# ---------------------------------------------------------------------------

class TestImportModeBundles:
    def test_basic_import(self, tmp_path: Path):
        zip_path = _make_gtfs_zip(tmp_path)
        db_path = tmp_path / "gtfs.db"
        import_mode_bundles(zip_path, db_path, "abc123", ["2"])
        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT COUNT(*) FROM stops").fetchone()
        conn.close()
        assert row[0] > 0

    def test_skips_already_imported(self, tmp_path: Path):
        zip_path = _make_gtfs_zip(tmp_path)
        db_path = tmp_path / "gtfs.db"
        import_mode_bundles(zip_path, db_path, "abc123", ["2"])
        # Second call with same bundle should be skipped (no error, no duplicate)
        import_mode_bundles(zip_path, db_path, "abc123", ["2"])
        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT COUNT(*) FROM stops").fetchone()
        conn.close()
        assert row[0] == 1  # only one stop in our fixture

    def test_force_reimports(self, tmp_path: Path):
        zip_path = _make_gtfs_zip(tmp_path)
        db_path = tmp_path / "gtfs.db"
        import_mode_bundles(zip_path, db_path, "sha1", ["2"])
        import_mode_bundles(zip_path, db_path, "sha2", ["2"], force=True)
        bundles = get_imported_bundles(db_path)
        assert "2" in bundles

    def test_stores_etag_and_size(self, tmp_path: Path):
        zip_path = _make_gtfs_zip(tmp_path)
        db_path = tmp_path / "gtfs.db"
        import_mode_bundles(
            zip_path, db_path, "abc", ["2"],
            zip_etag='"etag123"', zip_size="12345"
        )
        meta = get_stored_zip_meta(db_path)
        assert meta.get("zip_etag") == '"etag123"'
        assert meta.get("zip_size") == "12345"
        assert meta.get("zip_sha256") == "abc"

    def test_stop_times_filtered_to_active_trips(self, tmp_path: Path):
        zip_path = _make_gtfs_zip(tmp_path)
        db_path = tmp_path / "gtfs.db"
        import_mode_bundles(zip_path, db_path, "abc", ["2"])
        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM stop_times").fetchone()[0]
        conn.close()
        # Our fixture has 1 trip active today → 1 stop_time row
        assert count == 1


# ---------------------------------------------------------------------------
# get_imported_bundles / get_stored_zip_meta
# ---------------------------------------------------------------------------

class TestMetaHelpers:
    def test_empty_when_no_db(self, tmp_path: Path):
        assert get_imported_bundles(tmp_path / "nope.db") == set()
        assert get_stored_zip_meta(tmp_path / "nope.db") == {}

    def test_returns_imported_bundles(self, tmp_path: Path):
        zip_path = _make_gtfs_zip(tmp_path)
        db_path = tmp_path / "gtfs.db"
        import_mode_bundles(zip_path, db_path, "sha", ["2"])
        assert "2" in get_imported_bundles(db_path)

    def test_sha256_stored(self, tmp_path: Path):
        zip_path = _make_gtfs_zip(tmp_path)
        db_path = tmp_path / "gtfs.db"
        import_mode_bundles(zip_path, db_path, "deadbeef", ["2"])
        meta = get_stored_zip_meta(db_path)
        assert meta["zip_sha256"] == "deadbeef"
