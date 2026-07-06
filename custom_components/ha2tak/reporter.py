"""Periodic conversion of Home Assistant entity states into CoT events."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_BATTERY_LEVEL,
    ATTR_GPS_ACCURACY,
    ATTR_LATITUDE,
    ATTR_LONGITUDE,
    EVENT_HOMEASSISTANT_STARTED,
    EVENT_HOMEASSISTANT_STOP,
    STATE_HOME,
    STATE_NOT_HOME,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import CoreState, Event, HomeAssistant, State, callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from .const import (
    CONF_CALLSIGN,
    CONF_COT_TYPE,
    CONF_ENTITIES,
    CONF_INTERVAL,
    CONF_MODE,
    CONF_REMARKS,
    CONF_SEND_ON_UPDATE,
    CONF_STALE,
    CONF_TEAM_COLOR,
    CONF_TEAM_ROLE,
    CONTACT_COT_TYPE,
    DEFAULT_INTERVAL,
    DEFAULT_MARKER_TYPE,
    DEFAULT_STALE,
    DEFAULT_TEAM_COLOR,
    DEFAULT_TEAM_ROLE,
    MODE_CONTACT,
)
from .cot import build_cot
from .tak import TakConnection

_LOGGER = logging.getLogger(__name__)

# Minimum spacing between state-change-triggered reports per entity, so a
# chatty device_tracker cannot flood the TAK server between intervals.
IMMEDIATE_COOLDOWN = 5.0


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class TakReporter:
    """Reads configured entities and delivers their positions as CoT."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, connection: TakConnection
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.connection = connection
        self.last_report: datetime | None = None
        self.last_error: str | None = None
        self.events_sent: int = 0
        self._unsubs: list[Callable[[], None]] = []
        self._listeners: list[Callable[[], None]] = []
        self._last_immediate: dict[str, float] = {}
        self._report_lock = asyncio.Lock()

    @property
    def tracked_entities(self) -> dict[str, dict[str, Any]]:
        return self.entry.options.get(CONF_ENTITIES, {})

    async def async_start(self) -> None:
        interval = int(self.entry.options.get(CONF_INTERVAL, DEFAULT_INTERVAL))
        self._unsubs.append(
            async_track_time_interval(
                self.hass, self._async_interval, timedelta(seconds=interval)
            )
        )
        if self.entry.options.get(CONF_SEND_ON_UPDATE) and self.tracked_entities:
            self._unsubs.append(
                async_track_state_change_event(
                    self.hass,
                    list(self.tracked_entities),
                    self._async_state_changed,
                )
            )
        # Close the TAK session cleanly on shutdown so the contact drops
        # offline server-side instead of lingering until a TCP timeout.
        self._unsubs.append(
            self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STOP, self._async_on_stop
            )
        )
        if self.hass.state is CoreState.running:
            self.hass.async_create_task(self.async_report())
        else:
            self._unsubs.append(
                self.hass.bus.async_listen_once(
                    EVENT_HOMEASSISTANT_STARTED, self._async_started
                )
            )

    async def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        await self.connection.async_close()

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Subscribe a sensor to reporter state updates."""
        self._listeners.append(listener)

        def _remove() -> None:
            self._listeners.remove(listener)

        return _remove

    @callback
    def _notify(self) -> None:
        for listener in self._listeners:
            listener()

    async def _async_started(self, _event: Event) -> None:
        await self.async_report()

    async def _async_on_stop(self, _event: Event) -> None:
        await self.connection.async_close()

    async def _async_interval(self, _now: datetime) -> None:
        await self.async_report()

    async def _async_state_changed(self, event: Event) -> None:
        entity_id = event.data["entity_id"]
        new_state: State | None = event.data.get("new_state")
        if new_state is None or new_state.state in (
            STATE_UNKNOWN,
            STATE_UNAVAILABLE,
        ):
            return
        now = time.monotonic()
        if now - self._last_immediate.get(entity_id, 0.0) < IMMEDIATE_COOLDOWN:
            return
        self._last_immediate[entity_id] = now
        await self.async_report(only_entity=entity_id)

    async def async_report(
        self, _now: datetime | None = None, only_entity: str | None = None
    ) -> None:
        """Build CoT events for the configured entities and send them."""
        async with self._report_lock:
            events: list[bytes] = []
            for entity_id, cfg in self.tracked_entities.items():
                if only_entity is not None and entity_id != only_entity:
                    continue
                event = self._build_event(entity_id, cfg)
                if event is not None:
                    events.append(event)
            if not events:
                return
            try:
                await self.connection.async_send(events)
            except (OSError, asyncio.TimeoutError) as exc:
                self.last_error = str(exc)
                _LOGGER.warning(
                    "Could not deliver %d CoT event(s) to TAK server %s:%d: %s",
                    len(events),
                    self.connection.host,
                    self.connection.port,
                    exc,
                )
            else:
                self.last_report = dt_util.utcnow()
                self.last_error = None
                self.events_sent += len(events)
            self._notify()

    def _build_event(self, entity_id: str, cfg: dict[str, Any]) -> bytes | None:
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return None

        lat = _as_float(state.attributes.get(ATTR_LATITUDE))
        lon = _as_float(state.attributes.get(ATTR_LONGITUDE))
        if lat is None or lon is None:
            # Router-based trackers report only a zone name; fall back to the
            # zone's coordinates.
            zone = self._zone_for_state(state)
            if zone is None:
                _LOGGER.debug("No coordinates for %s (state: %s)", entity_id, state.state)
                return None
            lat = _as_float(zone.attributes.get(ATTR_LATITUDE))
            lon = _as_float(zone.attributes.get(ATTR_LONGITUDE))
            if lat is None or lon is None:
                return None

        battery = state.attributes.get(ATTR_BATTERY_LEVEL)
        if battery is None:
            battery = state.attributes.get("battery")
        battery_int = None
        if (battery_f := _as_float(battery)) is not None:
            battery_int = int(battery_f)

        contact = cfg.get(CONF_MODE, MODE_CONTACT) == MODE_CONTACT
        cot_type = (
            CONTACT_COT_TYPE
            if contact
            else cfg.get(CONF_COT_TYPE, DEFAULT_MARKER_TYPE)
        )
        return build_cot(
            uid=f"HA2TAK.{entity_id}",
            callsign=cfg.get(CONF_CALLSIGN) or state.name,
            lat=lat,
            lon=lon,
            cot_type=cot_type,
            contact_presence=contact,
            accuracy_m=_as_float(state.attributes.get(ATTR_GPS_ACCURACY)),
            altitude_m=_as_float(state.attributes.get("altitude")),
            course_deg=_as_float(state.attributes.get("course")),
            speed_ms=_as_float(state.attributes.get("speed")),
            battery=battery_int,
            fix_time=state.last_updated,
            stale_seconds=int(self.entry.options.get(CONF_STALE, DEFAULT_STALE)),
            remarks=cfg.get(CONF_REMARKS) or "",
            team_color=cfg.get(CONF_TEAM_COLOR, DEFAULT_TEAM_COLOR),
            team_role=cfg.get(CONF_TEAM_ROLE, DEFAULT_TEAM_ROLE),
        )

    def _zone_for_state(self, state: State) -> State | None:
        """Find the zone entity matching a person/device_tracker state."""
        if state.state == STATE_HOME:
            return self.hass.states.get("zone.home")
        if state.state == STATE_NOT_HOME:
            return None
        for zone in self.hass.states.async_all("zone"):
            if zone.name == state.state:
                return zone
        return None
