"""Per-config-entry runtime objects."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo

from .const import DOMAIN
from .reporter import TakReporter
from .tak import TakConnection
from .tracker import CallsignTracker


@dataclass
class Ha2TakRuntime:
    connection: TakConnection
    reporter: TakReporter
    tracker: CallsignTracker


def server_device_info(entry: ConfigEntry) -> DeviceInfo:
    """The device representing one TAK server connection."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer="TAK Product Center",
        model="TAK Server",
        entry_type=DeviceEntryType.SERVICE,
    )
