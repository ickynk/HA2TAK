"""Status sensor showing the last successful CoT report."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import Ha2TakConfigEntry
from .const import (
    ATTR_EVENTS_SENT,
    ATTR_LAST_ERROR,
    ATTR_TRACKED_ENTITIES,
    DOMAIN,
)
from .reporter import TakReporter


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Ha2TakConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([Ha2TakLastReportSensor(entry)])


class Ha2TakLastReportSensor(SensorEntity):
    """Timestamp of the last successful delivery to the TAK server."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_translation_key = "last_report"

    def __init__(self, entry: Ha2TakConfigEntry) -> None:
        self._reporter: TakReporter = entry.runtime_data
        self._attr_unique_id = f"{entry.entry_id}_last_report"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="TAK Product Center",
            model="TAK Server",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=None,
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._reporter.async_add_listener(self._on_update))

    @callback
    def _on_update(self) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> datetime | None:
        return self._reporter.last_report

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            ATTR_TRACKED_ENTITIES: sorted(self._reporter.tracked_entities),
            ATTR_EVENTS_SENT: self._reporter.events_sent,
            ATTR_LAST_ERROR: self._reporter.last_error,
        }
