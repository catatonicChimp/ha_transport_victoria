"""Constants for the Transport Victoria integration."""

DOMAIN = "transport_victoria"

# -------------------------------------------------------------------
# PTV Open Data API
# -------------------------------------------------------------------

GTFS_SCHEDULE_URL = (
    "https://opendata.transport.vic.gov.au/dataset/"
    "3f4e292e-7f8a-4ffe-831f-1953be0fe448/resource/"
    "fb152201-859f-4882-9206-b768060b50ad/download/gtfs.zip"
)

REALTIME_BASE_URL = (
    "https://api.opendata.transport.vic.gov.au"
    "/opendata/public-transport/gtfs/realtime/v1"
)

# Auth header name for the PTV Open Data API key
REALTIME_AUTH_HEADER = "KeyID"

# Rate limit: 24 requests per 60 seconds across ALL endpoints combined
RATE_LIMIT_CALLS = 24
RATE_LIMIT_PERIOD = 60  # seconds

# -------------------------------------------------------------------
# Realtime feed modes
# -------------------------------------------------------------------

MODE_METRO = "metro"
MODE_TRAM = "tram"
MODE_BUS = "bus"
MODE_REGIONAL_BUS = "regional-bus"
MODE_VLINE = "vline"

MODES = [MODE_METRO, MODE_TRAM, MODE_BUS, MODE_REGIONAL_BUS, MODE_VLINE]

# Human-readable labels for each mode (used in config flow)
MODE_LABELS: dict[str, str] = {
    MODE_METRO: "Metro Trains",
    MODE_TRAM: "Trams",
    MODE_BUS: "Buses",
    MODE_REGIONAL_BUS: "Regional Buses",
    MODE_VLINE: "V/Line",
}

# -------------------------------------------------------------------
# Realtime feed types
# -------------------------------------------------------------------

FEED_TRIP_UPDATES = "trip-updates"
FEED_SERVICE_ALERTS = "service-alerts"
FEED_VEHICLE_POSITIONS = "vehicle-positions"

# -------------------------------------------------------------------
# GTFS route_type codes used by PTV
#
# PTV uses a mix of standard and extended (HVT) route_type values.
# Discovered by inspecting each mode bundle inside the GTFS ZIP:
#   folder 1 → V/Line Rail          route_type 2
#   folder 2 → Metro Trains         route_type 400  (HVT: metro/underground)
#   folder 3 → Trams                route_type 0
#   folder 4 → Metro Buses          route_type 3, 701
#   folder 5 → V/Line Coaches       route_type 204  (HVT: international coach)
#   folder 6 → Regional Buses       route_type 701  (HVT: rural bus)
#   folder 10 → Ferry               route_type 102
#   folder 11 → Airport/Special     route_type 3
# -------------------------------------------------------------------

ROUTE_TYPE_TRAM = 0
ROUTE_TYPE_VLINE_RAIL = 2
ROUTE_TYPE_BUS = 3
ROUTE_TYPE_FERRY = 102
ROUTE_TYPE_VLINE_COACH = 204
ROUTE_TYPE_METRO_TRAIN = 400   # PTV HVT code — NOT the standard GTFS value of 1
ROUTE_TYPE_REGIONAL_BUS = 701

# Maps each mode to the folder number(s) inside the PTV outer ZIP.
# Used by the importer to only extract the bundles needed for a given mode.
#   folder 1  → V/Line Rail       (route_type 2)
#   folder 2  → Metro Trains      (route_type 400)
#   folder 3  → Trams             (route_type 0)
#   folder 4  → Metro Buses       (route_type 3, 701)
#   folder 5  → V/Line Coaches    (route_type 204)
#   folder 6  → Regional Buses    (route_type 701)
#   folder 10 → Ferry             (route_type 102)
#   folder 11 → Airport/Special   (route_type 3)
MODE_BUNDLE_MAP: dict[str, list[str]] = {
    MODE_METRO: ["2"],
    MODE_TRAM: ["3"],
    MODE_BUS: ["4", "11"],
    MODE_REGIONAL_BUS: ["6"],
    MODE_VLINE: ["1", "5"],
}

ROUTE_TYPE_MAP: dict[str, list[int]] = {
    MODE_METRO: [ROUTE_TYPE_METRO_TRAIN],                        # 400
    MODE_TRAM: [ROUTE_TYPE_TRAM],                                # 0
    MODE_BUS: [ROUTE_TYPE_BUS, ROUTE_TYPE_REGIONAL_BUS],         # 3, 701
    MODE_REGIONAL_BUS: [ROUTE_TYPE_REGIONAL_BUS, ROUTE_TYPE_VLINE_COACH],  # 701, 204
    MODE_VLINE: [ROUTE_TYPE_VLINE_RAIL, ROUTE_TYPE_VLINE_COACH], # 2, 204
}

# -------------------------------------------------------------------
# City Loop stop IDs
# Used to detect when origin/destination routing needs disambiguation.
# Stop IDs are strings in GTFS.
# -------------------------------------------------------------------

CITY_LOOP_STOP_IDS: frozenset[str] = frozenset({
    "19843",  # Flinders Street
    "19842",  # Southern Cross
    "19844",  # Melbourne Central
    "19845",  # Flagstaff
    "19846",  # Parliament
})

# -------------------------------------------------------------------
# Config entry data keys
# -------------------------------------------------------------------

CONF_API_KEY = "api_key"
CONF_MODE = "mode"
CONF_STOP_ID = "stop_id"
CONF_STOP_NAME = "stop_name"
CONF_DESTINATION_STOP_ID = "destination_stop_id"
CONF_DESTINATION_STOP_NAME = "destination_stop_name"
CONF_ALL_DESTINATIONS = "all_destinations"
CONF_WATCH_NAME = "watch_name"

# Options (user-adjustable after setup via the gear icon)
CONF_POLL_INTERVAL = "poll_interval"           # seconds: 15, 30, or 60
CONF_REALTIME_ENABLED = "realtime_enabled"     # bool: False disables RT fetches
CONF_DEPARTURE_COUNT = "departure_count"       # int: 1, 2, or 3 next services
DEFAULT_DEPARTURE_COUNT = 3

# Filtering (optional, set during config flow)
CONF_ROUTE_ID_FILTER = "route_id_filter"   # restrict to a specific route/line
CONF_PLATFORM_FILTER = "platform_filter"  # restrict to a specific platform code

# Multi-leg journey mode
CONF_JOURNEY_MODE = "journey_mode"
CONF_TRANSFER_STOP_ID = "transfer_stop_id"
CONF_TRANSFER_STOP_NAME = "transfer_stop_name"
CONF_JOURNEY_DESTINATION_STOP_ID = "journey_destination_stop_id"
CONF_JOURNEY_DESTINATION_STOP_NAME = "journey_destination_stop_name"
CONF_MIN_TRANSFER_MINUTES = "min_transfer_minutes"

# Entry type flag (stored in config data to distinguish entry types)
CONF_ENTRY_TYPE = "entry_type"
ENTRY_TYPE_WATCH = "watch"
ENTRY_TYPE_VEHICLE_TRACKER = "vehicle_tracker"
ENTRY_TYPE_ROUTE_TRACKER = "route_tracker"
CONF_PARENT_ENTRY_ID = "parent_entry_id"  # for vehicle tracker entries

# Route tracker config keys
CONF_ROUTE_ID = "route_id"                     # primary route being tracked
CONF_ROUTE_SHORT_NAME = "route_short_name"
CONF_ROUTE_LONG_NAME = "route_long_name"
CONF_ROUTE_COLOR = "route_color"               # hex without '#'
CONF_MAX_VEHICLES = "max_vehicles"             # number of device_tracker slots
CONF_REFERENCE_STOP_ID = "reference_stop_id"  # optional proximity filter
CONF_REFERENCE_STOP_NAME = "reference_stop_name"
CONF_REFERENCE_STOP_LAT = "reference_stop_lat"
CONF_REFERENCE_STOP_LON = "reference_stop_lon"
CONF_PROXIMITY_RADIUS_M = "proximity_radius_m"

DEFAULT_MAX_VEHICLES = 5

# -------------------------------------------------------------------
# Coordinator / polling
# -------------------------------------------------------------------

# Default poll interval for realtime trip updates (seconds)
DEFAULT_POLL_INTERVAL_SECONDS = 30

# Stagger between coordinator startup times to avoid burst (seconds per entry)
COORDINATOR_STAGGER_SECONDS = 3

# Service alerts are slow-moving — poll less frequently
ALERTS_POLL_INTERVAL_SECONDS = 300  # 5 minutes

# TTL for the module-level realtime feed cache
REALTIME_CACHE_TTL_SECONDS = 30

# -------------------------------------------------------------------
# Departure sensor slots
# -------------------------------------------------------------------

DEPARTURE_SLOTS = [1, 2, 3]

# -------------------------------------------------------------------
# Transport Victoria pictograms
# Served at /transport_victoria/pictograms/<filename> via async_setup.
# Map each mode to its official pictogram PNG filename (no extension).
# -------------------------------------------------------------------

PICTOGRAM_URL_BASE = "/transport_victoria/pictograms"

MODE_PICTOGRAM: dict[str, str] = {
    MODE_METRO:       "PICTO_MODE_Train",
    MODE_TRAM:        "PICTO_MODE_Tram",
    MODE_BUS:         "PICTO_MODE_Bus",
    MODE_REGIONAL_BUS: "PICTO_MODE_Coach",
    MODE_VLINE:       "PICTO_MODE_RegionalTrain",
}

# -------------------------------------------------------------------
# Data attribution
# GTFS Schedule is published by the Department of Transport and Planning
# under Creative Commons Attribution 4.0 (CC BY 4.0).
# Realtime feeds are served by the same open-data platform.
# -------------------------------------------------------------------

ATTRIBUTION = (
    "Data provided by Public Transport Victoria "
    "(Department of Transport and Planning, State of Victoria) "
    "· Licensed under CC BY 4.0"
)

# MDI icons for each mode — used by the dynamic icon property on departure sensors
MODE_MDI_ICON: dict[str, str] = {
    MODE_METRO:        "mdi:train",
    MODE_TRAM:         "mdi:tram",
    MODE_BUS:          "mdi:bus",
    MODE_REGIONAL_BUS: "mdi:bus-articulated-front",
    MODE_VLINE:        "mdi:train-variant",
}
