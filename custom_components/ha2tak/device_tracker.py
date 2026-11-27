"""GPS trackers for callsigns received from the TAK server."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.const import ATTR_BATTERY_LEVEL, ATTR_GPS_ACCURACY
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from . import Ha2TakConfigEntry
from .const import CONF_NAME
from .runtime import server_device_info
from .tracker import (
    CallsignTracker,
    async_remove_unwanted_callsign_entities,
    signal_callsign_update,
)

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
ATTR_FIX_TIME = "fix_time"
ATTR_FIX_AGE = "fix_age_s"
ATTR_REPORTED_CE = "reported_ce"
ATTR_POSITION_STALE = "position_stale"
ATTR_POSITION_TIME = "position_time"

_RESTORED_ATTRS = (
    ATTR_UID,
    ATTR_COT_TYPE,
    ATTR_TEAM,
    ATTR_ROLE,
    ATTR_ALTITUDE,
    ATTR_SPEED,
    ATTR_COURSE,
    ATTR_REMARKS,
    ATTR_LAST_SEEN,
    ATTR_FIX_TIME,
    ATTR_FIX_AGE,
    ATTR_REPORTED_CE,
    ATTR_POSITION_STALE,
    ATTR_POSITION_TIME,
)


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
    async_remove_unwanted_callsign_entities(
        hass, entry, "device_tracker", {e.unique_id for e in entities}
    )
    async_add_entities(entities)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class TakCallsignTracker(TrackerEntity, RestoreEntity):
    """The position of one TAK callsign, as reported via the TAK server.

    Position and accuracy only change when the CallsignTracker accepts a
    new, fresh and accurate fix; other reports update the attributes."""

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
        self._attr_unique_id = f"{entry.entry_id}_callsign_{key}"
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

        if self._key not in self._tracker.states:
            if (last := await self.async_get_last_state()) is not None:
                self._restore(last.attributes)
        self._render()

    @callback
    def _restore(self, attrs: dict[str, Any]) -> None:
        """Keep showing the last known position across restarts until the
        callsign reports a newer fix."""
        lat, lon = attrs.get("latitude"), attrs.get("longitude")
        if lat is None or lon is None:
            return
        accuracy = attrs.get(ATTR_GPS_ACCURACY)
        position_time = dt_util.parse_datetime(str(attrs.get(ATTR_POSITION_TIME) or ""))
        self._tracker.seed(
            self._key,
            float(lat),
            float(lon),
            float(accuracy) if accuracy is not None else None,
            position_time,
            dt_util.parse_datetime(str(attrs.get(ATTR_FIX_TIME) or "")),
        )
        self._battery = attrs.get(ATTR_BATTERY_LEVEL)
        for key in _RESTORED_ATTRS:
            if attrs.get(key) is not None:
                self._extra[key] = attrs[key]
        if stale_at := dt_util.parse_datetime(str(attrs.get(ATTR_STALE_AT) or "")):
            self._stale_at = stale_at
            self._schedule_stale_update()

    @callback
    def _async_handle_update(self) -> None:
        self._render()
        self.async_write_ha_state()

    @callback
    def _render(self) -> None:
        state = self._tracker.states.get(self._key)
        if state is None:
            return
        if state.latitude is not None and state.longitude is not None:
            self._attr_latitude = state.latitude
            self._attr_longitude = state.longitude
            accuracy = state.accuracy
            if accuracy is None:
                accuracy = self._tracker.max_accuracy
            # Never report a wider circle than the configured limit.
            self._attr_location_accuracy = min(accuracy, self._tracker.max_accuracy)
            self._extra[ATTR_POSITION_TIME] = _iso(state.fix_time)

        report = state.report
        if report is None:
            return  # restored only
        if report.battery is not None:
            self._battery = report.battery
        position_time = self._extra.get(ATTR_POSITION_TIME)
        self._extra = {
            ATTR_CALLSIGN: report.callsign,
            ATTR_UID: report.uid,
            ATTR_COT_TYPE: report.cot_type,
            ATTR_LAST_SEEN: _iso(report.time or dt_util.utcnow()),
            ATTR_FIX_TIME: _iso(report.fix_time),
            ATTR_FIX_AGE: round(report.fix_age_s, 1),
            ATTR_POSITION_STALE: bool(state.position_stale),
        }
        if position_time is not None:
            self._extra[ATTR_POSITION_TIME] = position_time
        for key, value in (
            (ATTR_REPORTED_CE, report.reported_ce),
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
