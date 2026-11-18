"""GPS trackers for callsigns received from the TAK server."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.const import ATTR_BATTERY_LEVEL, ATTR_GPS_ACCURACY
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from . import Ha2TakConfigEntry
from .const import CONF_NAME
from .cot import CotReport
from .runtime import server_device_info
from .tracker import CallsignTracker, signal_callsign_update

ATTR_CALLSIGN = "callsign"
ATTR_UID = "uid"
ATTR_COT_TYPE = "cot_type"
ATTR_TEAM = "team"
ATTR_ROLE = "role"
ATTR_ALTITUDE = "altitude"
ATTR_SPEED = "speed"
ATTR_COURSE = "course"
ATTR_REMARKS = "remarks"
ATTR_LAST_SEEN = "last_seen"
ATTR_STALE_AT = "stale_at"
ATTR_STALE = "stale"

UNIQUE_ID_MARKER = "_callsign_"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Ha2TakConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    tracker = entry.runtime_data.tracker
    entities = [
        TakCallsignTracker(entry, tracker, key, cfg)
        for key, cfg in tracker.watched.items()
    ]

    # Callsigns removed in the options flow leave registry entries behind.
    wanted = {entity.unique_id for entity in entities}
    registry = er.async_get(hass)
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if (
            reg_entry.domain == "device_tracker"
            and UNIQUE_ID_MARKER in reg_entry.unique_id
            and reg_entry.unique_id not in wanted
        ):
            registry.async_remove(reg_entry.entity_id)

    async_add_entities(entities)


class TakCallsignTracker(TrackerEntity, RestoreEntity):
    """The position of one TAK callsign, as reported via the TAK server."""

    _attr_should_poll = False
    _attr_source_type = SourceType.GPS
    # TrackerEntity defaults to a diagnostic entity without a device; these
    # are primary entities (e.g. to attach to a Person).
    _attr_entity_category = None
    _attr_icon = "mdi:crosshairs-gps"

    def __init__(
        self,
        entry: Ha2TakConfigEntry,
        tracker: CallsignTracker,
        key: str,
        cfg: dict[str, Any],
    ) -> None:
        self._tracker = tracker
        self._key = key
        self._attr_unique_id = f"{entry.entry_id}{UNIQUE_ID_MARKER}{key}"
        self._attr_name = cfg.get(CONF_NAME) or cfg["callsign"]
        self._attr_device_info = server_device_info(entry)
        self._battery: int | None = None
        self._stale_at: datetime | None = None
        self._unsub_stale: CALLBACK_TYPE | None = None
        self._extra: dict[str, Any] = {ATTR_CALLSIGN: cfg["callsign"]}

    @property
    def battery_level(self) -> int | None:
        return self._battery

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = dict(self._extra)
        if self._stale_at is not None:
            attrs[ATTR_STALE_AT] = self._stale_at.isoformat()
            attrs[ATTR_STALE] = dt_util.utcnow() >= self._stale_at
        return attrs

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_callsign_update(self._tracker.entry.entry_id, self._key),
                self._async_handle_update,
            )
        )
        self.async_on_remove(self._cancel_stale_timer)

        if (report := self._tracker.latest.get(self._key)) is not None:
            self._apply(report)
        elif (last := await self.async_get_last_state()) is not None:
            self._restore(last.attributes)

    @callback
    def _restore(self, attrs: dict[str, Any]) -> None:
        """Keep showing the last known position across restarts until the
        callsign reports again."""
        lat, lon = attrs.get("latitude"), attrs.get("longitude")
        if lat is None or lon is None:
            return
        self._attr_latitude = float(lat)
        self._attr_longitude = float(lon)
        self._attr_location_accuracy = float(attrs.get(ATTR_GPS_ACCURACY) or 0)
        self._battery = attrs.get(ATTR_BATTERY_LEVEL)
        for key in (
            ATTR_UID,
            ATTR_COT_TYPE,
            ATTR_TEAM,
            ATTR_ROLE,
            ATTR_ALTITUDE,
            ATTR_SPEED,
            ATTR_COURSE,
            ATTR_REMARKS,
            ATTR_LAST_SEEN,
        ):
            if attrs.get(key) is not None:
                self._extra[key] = attrs[key]
        if stale_at := dt_util.parse_datetime(str(attrs.get(ATTR_STALE_AT) or "")):
            self._stale_at = stale_at
            self._schedule_stale_update()

    @callback
    def _async_handle_update(self) -> None:
        if (report := self._tracker.latest.get(self._key)) is not None:
            self._apply(report)
            self.async_write_ha_state()

    @callback
    def _apply(self, report: CotReport) -> None:
        self._attr_latitude = report.lat
        self._attr_longitude = report.lon
        self._attr_location_accuracy = report.ce or 0
        if report.battery is not None:
            self._battery = report.battery
        self._extra = {
            ATTR_CALLSIGN: report.callsign,
            ATTR_UID: report.uid,
            ATTR_COT_TYPE: report.cot_type,
            ATTR_LAST_SEEN: (report.time or dt_util.utcnow()).isoformat(),
        }
        for key, value in (
            (ATTR_TEAM, report.team),
            (ATTR_ROLE, report.role),
            (ATTR_ALTITUDE, report.hae),
            (ATTR_SPEED, report.speed),
            (ATTR_COURSE, report.course),
            (ATTR_REMARKS, report.remarks),
        ):
            if value is not None:
                self._extra[key] = value
        self._stale_at = report.stale
        self._schedule_stale_update()

    @callback
    def _schedule_stale_update(self) -> None:
        """Rewrite the state when the report goes stale, so the `stale`
        attribute flips without waiting for the next report."""
        self._cancel_stale_timer()
        if self._stale_at is None or self._stale_at <= dt_util.utcnow():
            return

        @callback
        def _went_stale(_now: datetime) -> None:
            self._unsub_stale = None
            self.async_write_ha_state()

        self._unsub_stale = async_track_point_in_utc_time(
            self.hass, _went_stale, self._stale_at
        )

    @callback
    def _cancel_stale_timer(self) -> None:
        if self._unsub_stale is not None:
            self._unsub_stale()
            self._unsub_stale = None
