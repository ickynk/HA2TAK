"""Pull positions of selected TAK callsigns into Home Assistant."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from .const import (
    CONF_MAX_ACCURACY,
    CONF_MAX_FIX_AGE,
    CONF_TAK_CALLSIGNS,
    DEFAULT_MAX_ACCURACY,
    DEFAULT_MAX_FIX_AGE,
    DOMAIN,
)
from .cot import CotReport, parse_cot

_LOGGER = logging.getLogger(__name__)

# How many distinct callsigns to remember for the "add callsign" picker.
MAX_SEEN = 500


def callsign_key(callsign: str) -> str:
    """Callsigns are matched case-insensitively and ignoring outer spaces."""
    return callsign.strip().casefold()


def signal_callsign_update(entry_id: str, key: str) -> str:
    return f"{DOMAIN}_{entry_id}_callsign_{key}"


@dataclass
class SeenCallsign:
    callsign: str
    uid: str
    cot_type: str
    last_seen: datetime


@dataclass
class CallsignState:
    """What Home Assistant shows for one callsign.

    The position is only replaced by a report carrying a *new* fix that is
    fresh and accurate enough. A sender repeating an old fix (e.g. WearTAK
    without GPS, with ce growing over time) must not move the tracker or
    widen its accuracy: Home Assistant counts a zone as entered when
    ``distance - gps_accuracy < radius``, so an inflated accuracy alone
    "enters" zones kilometres away."""

    latitude: float | None = None
    longitude: float | None = None
    accuracy: float | None = None
    # Fix currently shown as the position.
    fix_time: datetime | None = None
    # Newest fix received, shown or not; older reports are replays.
    newest_fix_time: datetime | None = None
    # The latest report did not carry a usable new fix.
    position_stale: bool | None = None
    report: CotReport | None = None


class CallsignTracker:
    """Receives every CoT event from the server connection and keeps the
    state of each callsign the user asked to track."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.states: dict[str, CallsignState] = {}
        self.seen: dict[str, SeenCallsign] = {}
        self.events_received = 0

    @property
    def watched(self) -> dict[str, dict[str, Any]]:
        return self.entry.options.get(CONF_TAK_CALLSIGNS, {})

    @property
    def max_fix_age(self) -> float:
        return float(self.entry.options.get(CONF_MAX_FIX_AGE, DEFAULT_MAX_FIX_AGE))

    @property
    def max_accuracy(self) -> float:
        return float(self.entry.options.get(CONF_MAX_ACCURACY, DEFAULT_MAX_ACCURACY))

    @callback
    def seed(
        self,
        key: str,
        latitude: float,
        longitude: float,
        accuracy: float | None,
        fix_time: datetime | None,
        newest_fix_time: datetime | None = None,
    ) -> None:
        """Start from a restored position, so replays of the same or older
        fixes after a restart are recognised as such."""
        if key in self.states:
            return
        if accuracy is not None:
            accuracy = min(accuracy, self.max_accuracy)
        self.states[key] = CallsignState(
            latitude=latitude,
            longitude=longitude,
            accuracy=accuracy,
            fix_time=fix_time,
            newest_fix_time=max(
                (t for t in (fix_time, newest_fix_time) if t is not None),
                default=None,
            ),
        )

    @callback
    def handle_event(self, event: bytes) -> None:
        """Connection event handler: called in the event loop for each raw
        CoT event the server pushes to us."""
        if b"callsign" not in event:
            return  # cheap pre-filter: pings, chat acks, ...
        report = parse_cot(event)
        if report is None:
            return
        self.events_received += 1
        key = callsign_key(report.callsign)

        if key not in self.seen and len(self.seen) >= MAX_SEEN:
            oldest = min(self.seen, key=lambda k: self.seen[k].last_seen)
            del self.seen[oldest]
        self.seen[key] = SeenCallsign(
            callsign=report.callsign,
            uid=report.uid,
            cot_type=report.cot_type,
            last_seen=dt_util.utcnow(),
        )

        if key in self.watched and self._apply(key, report):
            async_dispatcher_send(
                self.hass, signal_callsign_update(self.entry.entry_id, key)
            )

    @callback
    def _apply(self, key: str, report: CotReport) -> bool:
        """Fold a report into the callsign's state. False if it was dropped."""
        state = self.states.setdefault(key, CallsignState())
        fix = report.fix_time

        if fix is not None and state.newest_fix_time is not None:
            if fix < state.newest_fix_time:
                _LOGGER.debug(
                    "%s: dropping report with fix %s older than %s "
                    "(replay or out of order)",
                    report.callsign,
                    fix.isoformat(),
                    state.newest_fix_time.isoformat(),
                )
                return False
            new_fix = fix > state.newest_fix_time
        else:
            new_fix = True
        if fix is not None:
            state.newest_fix_time = fix

        too_old = report.fix_age_s > self.max_fix_age
        too_vague = report.ce is not None and report.ce > self.max_accuracy
        if new_fix and not too_old and not too_vague:
            state.latitude = report.lat
            state.longitude = report.lon
            if report.ce is not None:
                state.accuracy = report.ce
            elif state.accuracy is None:
                # Unknown accuracy is not perfect accuracy.
                state.accuracy = self.max_accuracy
            state.fix_time = fix
            state.position_stale = False
        else:
            state.position_stale = True
            _LOGGER.debug(
                "%s: keeping last position (new fix: %s, fix age %.0fs, ce %s)",
                report.callsign,
                new_fix,
                report.fix_age_s,
                report.ce,
            )
        state.report = report
        return True

    def seen_callsigns(self) -> list[SeenCallsign]:
        """Recently seen callsigns, most recent first."""
        return sorted(self.seen.values(), key=lambda s: s.last_seen, reverse=True)


@callback
def async_remove_unwanted_callsign_entities(
    hass: HomeAssistant, entry: ConfigEntry, domain: str, wanted: set[str | None]
) -> None:
    """Callsigns removed in the options flow leave registry entries behind."""
    registry = er.async_get(hass)
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if (
            reg_entry.domain == domain
            and "_callsign_" in reg_entry.unique_id
            and reg_entry.unique_id not in wanted
        ):
            registry.async_remove(reg_entry.entity_id)
