# Transport Victoria for Home Assistant

[![Tests](https://github.com/ataylor/ha-transport-victoria/actions/workflows/tests.yaml/badge.svg)](https://github.com/ataylor/ha-transport-victoria/actions/workflows/tests.yaml)
[![HACS](https://github.com/ataylor/ha-transport-victoria/actions/workflows/hacs.yaml/badge.svg)](https://github.com/ataylor/ha-transport-victoria/actions/workflows/hacs.yaml)
[![Hassfest](https://github.com/ataylor/ha-transport-victoria/actions/workflows/hassfest.yaml/badge.svg)](https://github.com/ataylor/ha-transport-victoria/actions/workflows/hassfest.yaml)

A [Home Assistant](https://www.home-assistant.io/) custom integration for **Public Transport Victoria (PTV)** open data. It shows upcoming departures at a chosen stop (metro trains, trams, buses, regional buses, V/Line) and merges **GTFS schedule** data with **GTFS Realtime** delays and service alerts.

Schedule data is stored locally in SQLite after download; realtime feeds require a free API key from PTV Open Data.

---

## Features

- **Departure sensors** — next departures with scheduled time, delay, route, headsign, platform, and trip id where available.
- **Disruption sensor** — binary sensor when service alerts apply to your watch.
- **Config flow** — pick mode, find a stop by name, distance from home, or map location; optional specific destination routing.
- **Rate limiting** — shared async limiter so multiple watches stay within PTV’s API limits (24 requests / 60 seconds across all realtime endpoints).

---

## Requirements

- Home Assistant **2024.x** or newer (uses current config-entry patterns such as `runtime_data`).
- A **PTV Open Data** subscription key for realtime feeds (trip updates and service alerts). Same key used as HTTP header `KeyID`. [Get a key](https://opendata.transport.vic.gov.au) under **My Account → Subscription Keys** (self-service, no approval queue).

Static GTFS schedule downloads do not require a key.

---

## Installation

### HACS (recommended)

1. Open HACS → **Integrations** → **⋮** → **Custom repositories**.
2. Add this repository URL, category **Integration**.
3. Install **Transport Victoria** and restart Home Assistant.
4. **Settings → Devices & services → Add integration** → search **Transport Victoria**.

### Manual

Copy the `custom_components/transport_victoria` folder into your Home Assistant `config/custom_components/` directory, then restart Home Assistant and add the integration from the UI.

```text
config/
└── custom_components/
    └── transport_victoria/
        ├── manifest.json
        └── ...
```

---

## Configuration

Configuration is done entirely in the UI.

1. **API key** — validated with a test request to the realtime API.
2. **Mode** — e.g. Metro Trains, Trams, Buses, Regional Buses, V/Line.
3. **Stop** — search by name, list by distance from the [Home Assistant zone](https://www.home-assistant.io/integrations/zone/), or drop a pin on the map.
4. **Destination** — all destinations (default) or a specific destination with route validation.
5. **Name** — used for entity ids (e.g. `sensor.transport_victoria_<name>_next`).

On first use the integration downloads and imports the relevant GTFS bundle(s); this can take a minute. Data is refreshed on a weekly schedule and when you add modes that need new bundles.

**Options** (after setup): rename the watch, adjust polling interval, toggle realtime updates — see the integration’s **Configure** in the UI.

---

## Entities

Per **watch** (each config entry), you typically get:

| Entity | Description |
|--------|-------------|
| Departure sensors | Minutes to next departures (multiple slots: next, next_2, …) |
| Disruption binary sensor | On when an active service alert matches the watch |

Vehicle / route tracking entries (if enabled in your build) expose additional entities such as device trackers.

Exact entity ids follow Home Assistant’s naming; see **Developer tools → States** after setup.

---

## Data sources

| Data | Source |
|------|--------|
| Static timetables | [GTFS Schedule](https://opendata.transport.vic.gov.au/dataset/gtfs-schedule) (ZIP, updated regularly) |
| Delays & cancellations | GTFS Realtime trip updates |
| Disruptions | GTFS Realtime service alerts |

Dataset terms: **CC BY 4.0** where applicable — see [data.vic.gov.au / Transport](https://opendata.transport.vic.gov.au/organization/public-transport) for current licence text.

This integration is **not** affiliated with Transport Victoria or the Victorian Government.

---

## Development

Clone the repo and use the included Compose stack to run Home Assistant with this component mounted read-only.

**Prerequisites:** [Podman](https://podman.io/) with Compose (or Docker Compose with minor script tweaks). The helper scripts assume `podman compose`.

```bash
./scripts/dev.sh up      # start HA → http://localhost:8123
./scripts/dev.sh logs    # follow logs
./scripts/dev.sh restart # reload after code changes
./scripts/dev.sh reset   # wipe local dev config under dev/ (ignored by git)
./scripts/dev.sh down
```

The `dev/` directory is gitignored and holds your local `ha-config` and downloaded GTFS data.

**Tests** (Python 3.13 in CI):

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements_test.txt
pytest tests/ -q            # run from repository root so `custom_components` imports resolve
```

Strict typing: `mypy` is configured via `mypy.ini` for maintainers who run it locally.

---

## Project layout

```text
custom_components/transport_victoria/   # integration package
tests/                                    # pytest suite
.github/workflows/                       # CI: tests, HACS, hassfest
compose.yaml                              # local HA dev service
scripts/dev.sh                            # Podman-focused dev helpers
CLAUDE.md                                 # maintainer / agent context (architecture notes)
```

---

## Issues & contributions

Bug reports and pull requests are welcome: [Issues](https://github.com/ataylor/ha-transport-victoria/issues).

---

## Acknowledgements

GTFS and GTFS Realtime are industry-standard formats; this integration uses the **gtfs-realtime-bindings** Python package to decode protobuf feeds.
