"""Transport Victoria Home Assistant integration."""

from __future__ import annotations

import logging
import sqlite3
from datetime import timedelta
from pathlib import Path

from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    CONF_DEPARTURE_COUNT,
    CONF_ENTRY_TYPE,
    CONF_MODE,
    CONF_PARENT_ENTRY_ID,
    DEFAULT_DEPARTURE_COUNT,
    DOMAIN,
    ENTRY_TYPE_ROUTE_TRACKER,
    ENTRY_TYPE_VEHICLE_TRACKER,
    GTFS_SCHEDULE_URL,
    MODE_BUNDLE_MAP,
    PICTOGRAM_URL_BASE,
)
from .coordinator import PTVDataUpdateCoordinator
from .device_tracker import RouteVehicleCoordinator, VehicleTrackerCoordinator
from .gtfs.downloader import check_for_remote_changes, download_zip
from .gtfs.importer import get_imported_bundles, get_stored_zip_meta, import_mode_bundles
from .gtfs.repository import is_mode_ready

_LOGGER = logging.getLogger(__name__)

# Platforms registered for departure watch entries
_WATCH_PLATFORMS: list[str] = ["sensor", "binary_sensor"]

# Platforms registered for vehicle tracker entries
_TRACKER_PLATFORMS: list[str] = ["device_tracker"]

# Legacy alias (used by async_unload_platforms when entry_type not available)
PLATFORMS: list[str] = ["sensor", "binary_sensor"]

# How often to check for a new GTFS schedule ZIP
_GTFS_REFRESH_INTERVAL = timedelta(days=7)

# Subdirectory under hass.config.path() for all PTV data files
_DATA_DIR_NAME = "transport_victoria_data"


def _data_dir(hass: HomeAssistant) -> Path:
    return Path(hass.config.path(_DATA_DIR_NAME))


def _zip_path(hass: HomeAssistant) -> Path:
    return _data_dir(hass) / "gtfs.zip"


def _db_path(hass: HomeAssistant) -> Path:
    return _data_dir(hass) / "gtfs.db"


async def async_trigger_gtfs_refresh(
    hass: HomeAssistant,
    bundle_folders: list[str] | None = None,
    *,
    skip_download_if_exists: bool = False,
) -> None:
    """Download the GTFS ZIP if changed and import the requested bundles.

    Public so config_flow can trigger a refresh during first-time setup,
    before any config entry exists.

    Change detection uses a two-stage strategy:
      1. HEAD request comparing ETag + Content-Length — skips 282 MB download
         if the server reports the file is unchanged.
      2. SHA-256 comparison after download — final guard against partial changes.

    When the ZIP changes, all bundles are re-imported with force=True so stale
    rows from the previous GTFS export are replaced.

    Args:
        bundle_folders: Folder numbers to import (e.g. ["2"] for metro trains).
                        If None, imports all bundles needed by existing entries.
        skip_download_if_exists: Pass True from config-flow steps that know
                        the ZIP was already downloaded; avoids a redundant
                        282 MB HTTP request.
    """
    zip_path = _zip_path(hass)
    db_path = _db_path(hass)

    if bundle_folders is None:
        bundle_folders = _bundles_for_all_entries(hass)

    if not bundle_folders:
        _LOGGER.debug("No bundles to import")
        return

    # --- Stage 1: decide whether we even need to download ---
    stored_meta = await hass.async_add_executor_job(get_stored_zip_meta, db_path)
    stored_sha256 = stored_meta.get("zip_sha256")
    stored_etag = stored_meta.get("zip_etag")
    stored_size = stored_meta.get("zip_size")

    already_imported = await hass.async_add_executor_job(get_imported_bundles, db_path)
    all_bundles_present = all(b in already_imported for b in bundle_folders)

    if not skip_download_if_exists:
        # HEAD check: if ETag + size match and all bundles are imported, skip everything
        remote_changed = await check_for_remote_changes(
            hass, GTFS_SCHEDULE_URL, stored_etag, stored_size
        )
        if not remote_changed and all_bundles_present:
            _LOGGER.debug("GTFS schedule unchanged (HEAD check) — skipping download and import")
            return

    # --- Stage 2: download (or skip if file already on disk from this session) ---
    _LOGGER.debug("Downloading GTFS schedule")
    try:
        changed, sha256, etag, content_length = await download_zip(
            hass,
            GTFS_SCHEDULE_URL,
            zip_path,
            skip_if_exists=skip_download_if_exists,
        )
    except Exception as exc:
        _LOGGER.error("Failed to download GTFS schedule: %s", exc)
        return

    # If SHA-256 unchanged and all bundles are already imported, skip import
    if not changed and stored_sha256 == sha256 and all_bundles_present:
        _LOGGER.debug("GTFS schedule unchanged (SHA-256) and all bundles imported — skipping")
        if etag and (etag != stored_etag or content_length != stored_size):
            try:
                await hass.async_add_executor_job(
                    _update_zip_meta, db_path, etag, content_length
                )
            except Exception:
                pass
        return

    # ZIP has changed or bundles are missing — determine whether to force re-import
    zip_changed = changed and stored_sha256 != sha256
    _LOGGER.info(
        "Importing GTFS bundles %s (SHA-256: %s, force=%s)",
        bundle_folders,
        sha256,
        zip_changed,
    )

    def _run_import() -> None:
        import_mode_bundles(
            zip_path,
            db_path,
            sha256,
            bundle_folders,
            force=zip_changed,
            zip_etag=etag,
            zip_size=content_length,
        )

    try:
        await hass.async_add_executor_job(_run_import)
    except Exception as exc:
        _LOGGER.error("Failed to import GTFS bundles %s: %s", bundle_folders, exc)


def _update_zip_meta(db_path: Path, etag: str | None, size: str | None) -> None:
    """Store ETag and Content-Length in import_meta without touching other rows."""
    if not db_path.exists():
        return
    rows: list[tuple[str, str]] = []
    if etag:
        rows.append(("zip_etag", etag))
    if size:
        rows.append(("zip_size", size))
    if not rows:
        return
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO import_meta (key, value) VALUES (?, ?)", rows
        )
        conn.commit()
    finally:
        conn.close()


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """React to options changes.

    Departure count changes require a full reload to add/remove sensor entities.
    All other changes (poll interval, realtime toggle, name) are applied live.
    """
    stored = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    prev_count = stored.get("departure_count", DEFAULT_DEPARTURE_COUNT)
    new_count = int(entry.options.get(CONF_DEPARTURE_COUNT, DEFAULT_DEPARTURE_COUNT))

    if new_count != prev_count:
        await hass.config_entries.async_reload(entry.entry_id)
        return

    coordinator: PTVDataUpdateCoordinator = entry.runtime_data
    coordinator.async_update_listeners_for_options()


def _bundles_for_all_entries(hass: HomeAssistant) -> list[str]:
    """Return the union of bundle folders needed by all watch config entries."""
    seen: set[str] = set()
    result: list[str] = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        # Vehicle tracker entries don't have a mode/bundle to import
        entry_type = entry.data.get(CONF_ENTRY_TYPE)
        if entry_type in (ENTRY_TYPE_VEHICLE_TRACKER, ENTRY_TYPE_ROUTE_TRACKER):
            continue
        mode = entry.data.get(CONF_MODE, "")
        for folder in MODE_BUNDLE_MAP.get(mode, []):
            if folder not in seen:
                seen.add(folder)
                result.append(folder)
    return result


async def async_setup(hass: HomeAssistant, config: dict) -> bool:  # type: ignore[type-arg]
    """Register static file paths once at integration load time.

    Called by HA before any config entries are set up. Registers the
    transport_pictograms directory so entity_picture URLs work.
    """
    component_dir = Path(__file__).parent
    await hass.http.async_register_static_paths([
        StaticPathConfig(
            url_path=PICTOGRAM_URL_BASE,
            path=str(component_dir / "transport_pictograms"),
            cache_headers=True,
        ),
    ])
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate config entries to the current version.

    Version history:
      1 → 2: (reserved for future use — no data changes yet)
    """
    _LOGGER.debug(
        "Migrating PTV entry %s from version %s", entry.entry_id, entry.version
    )
    # No data migrations needed yet; bump the version and return True.
    if entry.version == 1:
        hass.config_entries.async_update_entry(entry, version=2)
        _LOGGER.info("Migrated PTV entry %s to version 2", entry.entry_id)
        return True

    _LOGGER.error(
        "Cannot migrate PTV entry %s from unsupported version %s",
        entry.entry_id,
        entry.version,
    )
    return False


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up PTV from a config entry."""
    _LOGGER.debug("Setting up PTV entry: %s", entry.entry_id)

    hass.data.setdefault(DOMAIN, {})

    entry_type = entry.data.get(CONF_ENTRY_TYPE, "watch")

    if entry_type == ENTRY_TYPE_VEHICLE_TRACKER:
        return await _async_setup_vehicle_tracker(hass, entry)

    if entry_type == ENTRY_TYPE_ROUTE_TRACKER:
        return await _async_setup_route_tracker(hass, entry)

    return await _async_setup_watch(hass, entry)


async def _async_setup_watch(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a departure watch config entry."""
    # Ensure the data directory exists
    _data_dir(hass).mkdir(parents=True, exist_ok=True)

    # Kick off a GTFS refresh if this mode's bundles aren't ready yet.
    mode = entry.data.get(CONF_MODE, "")
    bundles = MODE_BUNDLE_MAP.get(mode, [])
    if bundles and not is_mode_ready(_db_path(hass), bundles):
        hass.async_create_task(async_trigger_gtfs_refresh(hass, bundles))

    # Schedule weekly refreshes
    cancel_refresh = async_track_time_interval(
        hass,
        lambda _now: hass.async_create_task(async_trigger_gtfs_refresh(hass)),
        _GTFS_REFRESH_INTERVAL,
    )

    # Determine this entry's index for stagger calculation
    all_entries = hass.config_entries.async_entries(DOMAIN)
    entry_index = next(
        (i for i, e in enumerate(all_entries) if e.entry_id == entry.entry_id),
        0,
    )

    coordinator = PTVDataUpdateCoordinator(
        hass,
        entry,
        entry_index=entry_index,
        db_path=_db_path(hass),
    )

    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    cancel_options = entry.add_update_listener(_async_options_updated)

    hass.data[DOMAIN][entry.entry_id] = {
        "cancel_refresh": cancel_refresh,
        "cancel_options": cancel_options,
        "departure_count": int(entry.options.get(CONF_DEPARTURE_COUNT, DEFAULT_DEPARTURE_COUNT)),
    }

    await hass.config_entries.async_forward_entry_setups(entry, _WATCH_PLATFORMS)
    return True


async def _async_setup_route_tracker(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a route-based vehicle tracker config entry."""
    all_entries = hass.config_entries.async_entries(DOMAIN)
    entry_index = next(
        (i for i, e in enumerate(all_entries) if e.entry_id == entry.entry_id),
        0,
    )

    coordinator = RouteVehicleCoordinator(hass, entry, entry_index=entry_index)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    hass.data[DOMAIN][entry.entry_id] = {}

    await hass.config_entries.async_forward_entry_setups(entry, _TRACKER_PLATFORMS)
    return True


async def _async_setup_vehicle_tracker(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a vehicle tracker config entry."""
    parent_entry_id: str = entry.data.get(CONF_PARENT_ENTRY_ID, "")
    parent_entry = hass.config_entries.async_get_entry(parent_entry_id)
    if parent_entry is None:
        _LOGGER.error(
            "Vehicle tracker %s: parent watch entry %s not found",
            entry.entry_id,
            parent_entry_id,
        )
        return False

    parent_coordinator: PTVDataUpdateCoordinator = parent_entry.runtime_data

    all_entries = hass.config_entries.async_entries(DOMAIN)
    entry_index = next(
        (i for i, e in enumerate(all_entries) if e.entry_id == entry.entry_id),
        0,
    )

    coordinator = VehicleTrackerCoordinator(
        hass,
        entry,
        parent_coordinator=parent_coordinator,
        entry_index=entry_index,
    )

    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    hass.data[DOMAIN][entry.entry_id] = {}

    await hass.config_entries.async_forward_entry_setups(entry, _TRACKER_PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a PTV config entry."""
    _LOGGER.debug("Unloading PTV entry: %s", entry.entry_id)

    entry_type = entry.data.get(CONF_ENTRY_TYPE, "watch")
    platforms = (
        _TRACKER_PLATFORMS
        if entry_type in (ENTRY_TYPE_VEHICLE_TRACKER, ENTRY_TYPE_ROUTE_TRACKER)
        else _WATCH_PLATFORMS
    )

    unload_ok = await hass.config_entries.async_unload_platforms(entry, platforms)

    if unload_ok:
        entry_data = hass.data[DOMAIN].pop(entry.entry_id, {})
        for key in ("cancel_refresh", "cancel_options"):
            cancel = entry_data.get(key)
            if cancel:
                cancel()

    if not hass.config_entries.async_entries(DOMAIN):
        hass.data.pop(DOMAIN, None)

    return bool(unload_ok)
