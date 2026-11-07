"""HA2TAK - report Home Assistant entity locations to a TAK server as CoT."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
)
from homeassistant.util import dt as dt_util

from .const import (
    AUTH_ENROLLMENT,
    CONF_AUTH_METHOD,
    CONF_ENROLLED_CERT,
    DOMAIN,
    RENEW_BEFORE_DAYS,
    SERVICE_SEND_NOW,
)
from .enrollment import (
    EnrollmentAuthError,
    EnrollmentError,
    async_enroll_from_config,
    cert_expiry,
)
from .reporter import TakReporter
from .tak import TakConfigError, TakConnection

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]

Ha2TakConfigEntry = ConfigEntry[TakReporter]


async def async_setup_entry(hass: HomeAssistant, entry: Ha2TakConfigEntry) -> bool:
    """Set up a TAK server connection from a config entry."""
    if entry.data.get(CONF_AUTH_METHOD) == AUTH_ENROLLMENT:
        await _async_renew_if_needed(hass, entry)
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


async def _async_renew_if_needed(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Re-enroll with the stored username/password when the client
    certificate is missing, expired or close to expiry."""
    cert_pem = entry.data.get(CONF_ENROLLED_CERT)
    expires = None
    if cert_pem:
        try:
            expires = cert_expiry(cert_pem)
        except ValueError:
            expires = None
    now = dt_util.utcnow()
    if expires is not None and expires - now > timedelta(days=RENEW_BEFORE_DAYS):
        return

    _LOGGER.info(
        "TAK client certificate for %s %s; re-enrolling",
        entry.title,
        f"expires {expires.isoformat()}" if expires else "is missing",
    )
    try:
        certs = await async_enroll_from_config(hass, dict(entry.data))
    except EnrollmentAuthError as exc:
        raise ConfigEntryAuthFailed(str(exc)) from exc
    except (EnrollmentError, TakConfigError) as exc:
        if expires is not None and expires > now:
            # Still valid: keep using it and retry at the next start/reload.
            _LOGGER.warning("Certificate renewal failed, will retry: %s", exc)
            return
        raise ConfigEntryNotReady(f"Certificate enrollment failed: {exc}") from exc
    hass.config_entries.async_update_entry(entry, data={**entry.data, **certs})


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
