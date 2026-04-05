"""Transport Victoria disruption binary sensor.

One binary sensor per watch — on when there is at least one active service
alert affecting the configured stop or route. Attributes carry the first
alert's details for use in HA notifications.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTRIBUTION, CONF_MODE, CONF_STOP_NAME, CONF_WATCH_NAME, DOMAIN, MODE_PICTOGRAM, PICTOGRAM_URL_BASE
from .coordinator import PTVDataUpdateCoordinator
from .models import Alert, CoordinatorData

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the disruption binary sensor for a config entry."""
    coordinator: PTVDataUpdateCoordinator = entry.runtime_data
    async_add_entities([PTVDisruptionBinarySensor(coordinator, entry)])


class PTVDisruptionBinarySensor(
    CoordinatorEntity[PTVDataUpdateCoordinator], BinarySensorEntity  # type: ignore[misc]
):
    """Binary sensor: on when service alerts are active for this watch."""

    _attr_has_entity_name = True
    _attr_name = "Disruption"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_attribution = ATTRIBUTION
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: PTVDataUpdateCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_disruption"
        mode = entry.data.get(CONF_MODE, "")
        picto = MODE_PICTOGRAM.get(mode)
        self._attr_entity_picture = f"{PICTOGRAM_URL_BASE}/{picto}.png" if picto else None

    @property
    def device_info(self) -> DeviceInfo:
        """Share the device with the departure sensors."""
        watch_name = self._entry.data.get(CONF_WATCH_NAME, self._entry.entry_id)
        stop_name = self._entry.data.get(CONF_STOP_NAME, "")
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=watch_name,
            manufacturer="Transport Victoria",
            model=stop_name,
        )

    @property
    def is_on(self) -> bool:
        data: CoordinatorData | None = self.coordinator.data
        return bool(data and data.alerts)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data: CoordinatorData | None = self.coordinator.data
        if not data or not data.alerts:
            return {}

        alert: Alert = data.alerts[0]
        attrs: dict[str, Any] = {
            "header": alert.header,
            "description": alert.description,
            "cause": alert.cause,
            "effect": alert.effect,
            "alert_count": len(data.alerts),
        }
        if alert.active_from:
            attrs["active_from"] = alert.active_from.isoformat()
        if alert.active_until:
            attrs["active_until"] = alert.active_until.isoformat()
        return attrs
