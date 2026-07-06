"""HA2TAK - report Home Assistant entity locations to a TAK server as CoT."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady

from .const import DOMAIN, SERVICE_SEND_NOW
from .reporter import TakReporter
from .tak import TakConfigError, TakConnection

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]

Ha2TakConfigEntry = ConfigEntry[TakReporter]


async def async_setup_entry(hass: HomeAssistant, entry: Ha2TakConfigEntry) -> bool:
    """Set up a TAK server connection from a config entry."""
    connection = TakConnection(hass, dict(entry.data))
    try:
        await connection.async_connect()
    except TakConfigError as exc:
        raise ConfigEntryError(str(exc)) from exc
    except (OSError, asyncio.TimeoutError) as exc:
        raise ConfigEntryNotReady(
            f"Cannot reach TAK server {connection.host}:{connection.port}: {exc}"
        ) from exc

    reporter = TakReporter(hass, entry, connection)
    entry.runtime_data = reporter
    await reporter.async_start()

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _async_register_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: Ha2TakConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.async_stop()
    return unload_ok


async def _async_update_listener(
    hass: HomeAssistant, entry: Ha2TakConfigEntry
) -> None:
    """Reload the entry when options (entities, interval) change."""
    await hass.config_entries.async_reload(entry.entry_id)


def _async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_SEND_NOW):
        return

    async def _handle_send_now(_call: ServiceCall) -> None:
        for entry in hass.config_entries.async_entries(DOMAIN):
            if entry.state is ConfigEntryState.LOADED:
                await entry.runtime_data.async_report()

    hass.services.async_register(DOMAIN, SERVICE_SEND_NOW, _handle_send_now)
