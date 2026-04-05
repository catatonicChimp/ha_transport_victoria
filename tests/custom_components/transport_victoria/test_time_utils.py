"""Unit tests for gtfs/time_utils.py."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from custom_components.transport_victoria.gtfs.time_utils import (
    day_of_week_column,
    departure_seconds_to_datetime,
    gtfs_time_to_seconds,
    seconds_to_time_str,
    service_date_str,
    service_day_for_time,
)


# ---------------------------------------------------------------------------
# gtfs_time_to_seconds
# ---------------------------------------------------------------------------

class TestGtfsTimeToSeconds:
    def test_midnight(self):
        assert gtfs_time_to_seconds("00:00:00") == 0

    def test_normal_time(self):
        assert gtfs_time_to_seconds("08:30:00") == 8 * 3600 + 30 * 60

    def test_end_of_day(self):
        assert gtfs_time_to_seconds("23:59:59") == 23 * 3600 + 59 * 60 + 59

    def test_post_midnight(self):
        # 25:30:00 = 1:30 AM next service day
        assert gtfs_time_to_seconds("25:30:00") == 25 * 3600 + 30 * 60

    def test_exactly_24_hours(self):
        assert gtfs_time_to_seconds("24:00:00") == 86400

    def test_whitespace_stripped(self):
        assert gtfs_time_to_seconds("  09:00:00  ") == 9 * 3600

    def test_invalid_format_raises(self):
        with pytest.raises(ValueError):
            gtfs_time_to_seconds("9:00")

    def test_invalid_format_missing_part_raises(self):
        with pytest.raises(ValueError):
            gtfs_time_to_seconds("09:00")


# ---------------------------------------------------------------------------
# seconds_to_time_str (roundtrip)
# ---------------------------------------------------------------------------

class TestSecondsToTimeStr:
    def test_midnight(self):
        assert seconds_to_time_str(0) == "00:00:00"

    def test_normal(self):
        assert seconds_to_time_str(8 * 3600 + 30 * 60) == "08:30:00"

    def test_post_midnight(self):
        assert seconds_to_time_str(25 * 3600 + 30 * 60) == "25:30:00"

    def test_roundtrip(self):
        for time_str in ("00:00:00", "08:30:45", "23:59:59", "25:15:00"):
            assert seconds_to_time_str(gtfs_time_to_seconds(time_str)) == time_str


# ---------------------------------------------------------------------------
# service_day_for_time
# ---------------------------------------------------------------------------

class TestServiceDayForTime:
    def test_morning_is_today(self):
        now = datetime(2024, 3, 15, 8, 30, 0)  # 08:30 Friday
        service_date, now_seconds = service_day_for_time(now)
        assert service_date == date(2024, 3, 15)
        assert now_seconds == 8 * 3600 + 30 * 60

    def test_just_before_cutover_is_yesterday(self):
        # 02:59 → still yesterday's service day
        now = datetime(2024, 3, 15, 2, 59, 0)
        service_date, now_seconds = service_day_for_time(now)
        assert service_date == date(2024, 3, 14)
        # 86400 + 2*3600 + 59*60 = 86400 + 10740 = 97140
        assert now_seconds == 86400 + 2 * 3600 + 59 * 60

    def test_exactly_at_cutover_is_today(self):
        # 03:00 → today's service day
        now = datetime(2024, 3, 15, 3, 0, 0)
        service_date, now_seconds = service_day_for_time(now)
        assert service_date == date(2024, 3, 15)
        assert now_seconds == 3 * 3600

    def test_midnight_is_yesterday(self):
        now = datetime(2024, 3, 15, 0, 0, 0)
        service_date, now_seconds = service_day_for_time(now)
        assert service_date == date(2024, 3, 14)
        assert now_seconds == 86400


# ---------------------------------------------------------------------------
# service_date_str
# ---------------------------------------------------------------------------

class TestServiceDateStr:
    def test_format(self):
        assert service_date_str(date(2024, 3, 5)) == "20240305"

    def test_year_boundary(self):
        assert service_date_str(date(2024, 1, 1)) == "20240101"


# ---------------------------------------------------------------------------
# day_of_week_column
# ---------------------------------------------------------------------------

class TestDayOfWeekColumn:
    def test_monday(self):
        assert day_of_week_column(date(2024, 3, 11)) == "monday"

    def test_friday(self):
        assert day_of_week_column(date(2024, 3, 15)) == "friday"

    def test_sunday(self):
        assert day_of_week_column(date(2024, 3, 17)) == "sunday"


# ---------------------------------------------------------------------------
# departure_seconds_to_datetime
# ---------------------------------------------------------------------------

class TestDepartureSecondsToDatetime:
    def test_normal_departure(self):
        result = departure_seconds_to_datetime(date(2024, 3, 15), 8 * 3600 + 30 * 60)
        assert result == datetime(2024, 3, 15, 8, 30, 0)

    def test_post_midnight_departure(self):
        # departure_seconds=91800 = 25*3600+30*60 → 01:30 on the next calendar day
        result = departure_seconds_to_datetime(date(2024, 3, 15), 25 * 3600 + 30 * 60)
        assert result == datetime(2024, 3, 16, 1, 30, 0)

    def test_midnight(self):
        result = departure_seconds_to_datetime(date(2024, 3, 15), 0)
        assert result == datetime(2024, 3, 15, 0, 0, 0)
