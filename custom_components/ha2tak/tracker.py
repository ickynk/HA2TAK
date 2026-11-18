"""Pull positions of selected TAK callsigns into Home Assistant."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from .const import CONF_TAK_CALLSIGNS, DOMAIN
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


class CallsignTracker:
    """Receives every CoT event from the server connection and keeps the
    latest position of each callsign the user asked to track."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.latest: dict[str, CotReport] = {}
        self.seen: dict[str, SeenCallsign] = {}
        self.events_received = 0

    @property
    def watched(self) -> dict[str, dict[str, Any]]:
        return self.entry.options.get(CONF_TAK_CALLSIGNS, {})

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

        if key in self.watched:
            self.latest[key] = report
            _LOGGER.debug(
                "%s (%s): %.6f, %.6f", report.callsign, report.uid, report.lat, report.lon
            )
            async_dispatcher_send(
                self.hass, signal_callsign_update(self.entry.entry_id, key)
            )

    def seen_callsigns(self) -> list[SeenCallsign]:
        """Recently seen callsigns, most recent first."""
        return sorted(self.seen.values(), key=lambda s: s.last_seen, reverse=True)
