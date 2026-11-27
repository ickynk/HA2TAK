"""Per-callsign "GPS fresh" sensors for callsigns received from TAK."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import Ha2TakConfigEntry
from .const import CONF_NAME
from .runtime import server_device_info
from .tracker import (
    CallsignTracker,
    async_remove_unwanted_callsign_entities,
    signal_callsign_update,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Ha2TakConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    tracker = entry.runtime_data.tracker
    entities = [
        TakGpsFreshSensor(entry, tracker, key, cfg)
        for key, cfg in tracker.watched.items()
    ]
    async_remove_unwanted_callsign_entities(
        hass, entry, "binary_sensor", {e.unique_id for e in entities}
    )
    async_add_entities(entities)


class TakGpsFreshSensor(BinarySensorEntity):
    """On while the callsign's latest report carried a new, fresh and
    accurate enough fix; off while the tracker holds its last good
    position. Unknown until the first report."""

    _attr_should_poll = False

    def __init__(
        self,
        entry: Ha2TakConfigEntry,
        tracker: CallsignTracker,
        key: str,
        cfg: dict[str, Any],
    ) -> None:
        self._tracker = tracker
        self._key = key
        self._attr_unique_id = f"{entry.entry_id}_callsign_{key}_gps_fresh"
        self._attr_name = f"{cfg.get(CONF_NAME) or cfg['callsign']} GPS fresh"
        self._attr_device_info = server_device_info(entry)

    @property
    def is_on(self) -> bool | None:
        state = self._tracker.states.get(self._key)
        if state is None or state.position_stale is None:
            return None
        return not state.position_stale

    @property
    def icon(self) -> str:
        return "mdi:crosshairs-gps" if self.is_on else "mdi:crosshairs-question"

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_callsign_update(self._tracker.entry.entry_id, self._key),
                self.async_write_ha_state,
            )
        )
