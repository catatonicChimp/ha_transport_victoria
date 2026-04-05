-- PTV GTFS SQLite schema
-- All ID columns are TEXT — GTFS IDs look numeric but must be treated as strings.
-- departure_seconds and arrival_seconds store HH:MM:SS times as total seconds
-- from service-day midnight, allowing post-midnight values (e.g. 25:30:00 = 91800).
-- This makes time comparisons fast integer operations on an indexed column.

PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = OFF;  -- not enforced for bulk import speed

-- ---------------------------------------------------------------
-- agency.txt
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agency (
    agency_id       TEXT PRIMARY KEY,
    agency_name     TEXT NOT NULL,
    agency_url      TEXT,
    agency_timezone TEXT,
    agency_lang     TEXT,
    agency_phone    TEXT
);

-- ---------------------------------------------------------------
-- routes.txt
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS routes (
    route_id         TEXT PRIMARY KEY,
    agency_id        TEXT,
    route_short_name TEXT,
    route_long_name  TEXT,
    route_type       INTEGER NOT NULL,  -- 0=Tram 1=Train 2=VLine 3=Bus 4=Ferry
    route_color      TEXT,
    route_text_color TEXT
);

CREATE INDEX IF NOT EXISTS idx_routes_type ON routes (route_type);
CREATE INDEX IF NOT EXISTS idx_routes_agency ON routes (agency_id);

-- ---------------------------------------------------------------
-- trips.txt
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trips (
    trip_id       TEXT PRIMARY KEY,
    route_id      TEXT NOT NULL,
    service_id    TEXT NOT NULL,
    shape_id      TEXT,
    trip_headsign TEXT,
    direction_id  INTEGER,
    block_id      TEXT
);

CREATE INDEX IF NOT EXISTS idx_trips_route ON trips (route_id);
CREATE INDEX IF NOT EXISTS idx_trips_service ON trips (service_id);

-- ---------------------------------------------------------------
-- stops.txt
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stops (
    stop_id             TEXT PRIMARY KEY,
    stop_name           TEXT NOT NULL,
    stop_lat            REAL,
    stop_lon            REAL,
    stop_code           TEXT,
    location_type       INTEGER DEFAULT 0,  -- 0=platform 1=station 2=entrance
    parent_station      TEXT,
    platform_code       TEXT,
    wheelchair_boarding INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_stops_name ON stops (stop_name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_stops_location ON stops (stop_lat, stop_lon);
CREATE INDEX IF NOT EXISTS idx_stops_parent ON stops (parent_station);

-- ---------------------------------------------------------------
-- stop_times.txt  (largest table — indexes are critical)
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stop_times (
    trip_id           TEXT NOT NULL,
    stop_id           TEXT NOT NULL,
    stop_sequence     INTEGER NOT NULL,
    arrival_seconds   INTEGER,  -- HH:MM:SS converted to total seconds from midnight
    departure_seconds INTEGER,  -- may be > 86400 for post-midnight departures
    pickup_type       INTEGER DEFAULT 0,
    drop_off_type     INTEGER DEFAULT 0,
    timepoint         INTEGER DEFAULT 1,
    PRIMARY KEY (trip_id, stop_sequence)
);

-- Primary query pattern: all departures from a stop after a given time
CREATE INDEX IF NOT EXISTS idx_stop_times_stop_depart
    ON stop_times (stop_id, departure_seconds);

-- Used when filtering by trip_id (realtime join, shape lookup)
CREATE INDEX IF NOT EXISTS idx_stop_times_trip
    ON stop_times (trip_id);

-- ---------------------------------------------------------------
-- calendar.txt
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS calendar (
    service_id TEXT PRIMARY KEY,
    monday     INTEGER NOT NULL,
    tuesday    INTEGER NOT NULL,
    wednesday  INTEGER NOT NULL,
    thursday   INTEGER NOT NULL,
    friday     INTEGER NOT NULL,
    saturday   INTEGER NOT NULL,
    sunday     INTEGER NOT NULL,
    start_date TEXT NOT NULL,  -- YYYYMMDD
    end_date   TEXT NOT NULL   -- YYYYMMDD
);

CREATE INDEX IF NOT EXISTS idx_calendar_dates ON calendar (start_date, end_date);

-- ---------------------------------------------------------------
-- calendar_dates.txt
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS calendar_dates (
    service_id     TEXT NOT NULL,
    date           TEXT NOT NULL,  -- YYYYMMDD
    exception_type INTEGER NOT NULL,  -- 1=added 2=removed
    PRIMARY KEY (service_id, date)
);

CREATE INDEX IF NOT EXISTS idx_calendar_dates_date ON calendar_dates (date);

-- ---------------------------------------------------------------
-- Meta table — tracks import state
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS import_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- Stored keys:
--   'zip_sha256'    — SHA-256 of the last imported ZIP
--   'zip_etag'      — ETag header from last successful download
--   'zip_size'      — Content-Length header from last successful download
--   'imported_at'   — ISO8601 timestamp of last successful import
--   'bundle_N'      — ISO8601 timestamp for each imported bundle folder
-- shapes.txt is intentionally NOT imported — it contains 11M geometry rows
-- needed only for map rendering, which adds no value for departure sensors.
