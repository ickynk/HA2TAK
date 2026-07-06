"""End-to-end tests: config flow, options flow and CoT delivery."""

from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.ha2tak.const import (
    CONF_CALLSIGN,
    CONF_COT_TYPE,
    CONF_ENTITIES,
    CONF_INTERVAL,
    CONF_MODE,
    CONF_PROTOCOL,
    CONF_REMARKS,
    CONF_SEND_ON_UPDATE,
    CONF_STALE,
    CONF_TEAM_COLOR,
    CONF_TEAM_ROLE,
    CONF_TLS_CHECK_HOSTNAME,
    CONF_TLS_VERIFY,
    DOMAIN,
    MODE_CONTACT,
    MODE_MARKER,
    SERVICE_SEND_NOW,
)


def _connection_input(fake_tak_server) -> dict:
    return {
        CONF_HOST: fake_tak_server.host,
        CONF_PORT: fake_tak_server.port,
        CONF_PROTOCOL: "tcp",
        CONF_TLS_VERIFY: True,
        CONF_TLS_CHECK_HOSTNAME: False,
    }


async def _wait_for_events(fake_tak_server, minimum: int = 1) -> list[ET.Element]:
    for _ in range(100):
        blob = b"".join(fake_tak_server.received)
        events = blob.split(b"<?xml")
        parsed = [
            ET.fromstring(b"<?xml" + chunk) for chunk in events if chunk.strip()
        ]
        if len(parsed) >= minimum:
            return parsed
        await asyncio.sleep(0.05)
    raise AssertionError(
        f"TAK server never received {minimum} event(s); got "
        f"{b''.join(fake_tak_server.received)!r}"
    )


async def _setup_entry(hass: HomeAssistant, fake_tak_server):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _connection_input(fake_tak_server)
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    await hass.async_block_till_done()
    entry = result["result"]
    assert entry.state is ConfigEntryState.LOADED
    return entry


async def test_config_flow_cannot_connect(hass: HomeAssistant, socket_enabled):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_HOST: "127.0.0.1",
            CONF_PORT: 1,  # nothing listens here
            CONF_PROTOCOL: "tcp",
            CONF_TLS_VERIFY: True,
            CONF_TLS_CHECK_HOSTNAME: False,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["base"] == "cannot_connect"


async def test_full_flow_contact_and_marker(hass: HomeAssistant, fake_tak_server):
    entry = await _setup_entry(hass, fake_tak_server)

    hass.states.async_set(
        "person.nick",
        "home",
        {
            "latitude": 51.5007325,
            "longitude": -0.1272003,
            "gps_accuracy": 15,
            "friendly_name": "Nick",
        },
    )

    # Add a contact-mode entity through the options flow.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_entity"}
    )
    assert result["step_id"] == "add_entity"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"entity_id": "person.nick", CONF_MODE: MODE_CONTACT}
    )
    assert result["step_id"] == "contact"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_CALLSIGN: "NICK",
            CONF_TEAM_COLOR: "Red",
            CONF_TEAM_ROLE: "Medic",
            CONF_REMARKS: "",
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options[CONF_ENTITIES]["person.nick"][CONF_CALLSIGN] == "NICK"

    # The reload triggers an immediate report of the new entity.
    events = await _wait_for_events(fake_tak_server)
    root = events[-1]
    assert root.get("uid") == "HA2TAK.person.nick"
    assert root.get("type") == "a-f-G-U-C"
    detail = root.find("detail")
    assert detail.find("contact").get("callsign") == "NICK"
    assert detail.find("__group").get("name") == "Red"
    assert root.find("point").get("lat") == "51.5007325"

    # Add a marker-mode entity (a zone).
    hass.states.async_set(
        "zone.cache",
        "0",
        {"latitude": 10.0, "longitude": 20.0, "friendly_name": "Cache"},
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_entity"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"entity_id": "zone.cache", CONF_MODE: MODE_MARKER}
    )
    assert result["step_id"] == "marker"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_CALLSIGN: "Supply cache", CONF_COT_TYPE: "a-h-G", CONF_REMARKS: "x"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    fake_tak_server.received.clear()
    await hass.services.async_call(DOMAIN, SERVICE_SEND_NOW, blocking=True)
    events = await _wait_for_events(fake_tak_server, minimum=2)
    by_uid = {e.get("uid"): e for e in events}
    marker = by_uid["HA2TAK.zone.cache"]
    assert marker.get("type") == "a-h-G"
    assert marker.find("detail/contact").get("callsign") == "Supply cache"
    assert marker.find("detail/takv") is None
    assert marker.find("detail/remarks").text == "x"
    assert marker.find("point").get("lat") == "10.0000000"
    assert by_uid["HA2TAK.person.nick"].find("detail/takv") is not None


async def test_settings_edit_and_remove(hass: HomeAssistant, fake_tak_server):
    entry = await _setup_entry(hass, fake_tak_server)

    # Reporting settings.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "settings"}
    )
    assert result["step_id"] == "settings"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_INTERVAL: 30, CONF_STALE: 120, CONF_SEND_ON_UPDATE: True},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options[CONF_INTERVAL] == 30
    assert entry.options[CONF_STALE] == 120
    assert entry.options[CONF_SEND_ON_UPDATE] is True

    # Add an entity, then edit it into a marker.
    hass.states.async_set(
        "device_tracker.car", "not_home", {"latitude": 1.0, "longitude": 2.0}
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_entity"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"entity_id": "device_tracker.car", CONF_MODE: MODE_CONTACT},
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_CALLSIGN: "CAR",
            CONF_TEAM_COLOR: "Blue",
            CONF_TEAM_ROLE: "Team Member",
            CONF_REMARKS: "",
        },
    )
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "edit_entity"}
    )
    assert result["step_id"] == "edit_entity"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"entity_id": "device_tracker.car"}
    )
    assert result["step_id"] == "mode"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_MODE: MODE_MARKER}
    )
    assert result["step_id"] == "marker"
    # Call sign default is carried over from the previous configuration.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_CALLSIGN: "CAR", CONF_COT_TYPE: "a-f-G-E-V-C", CONF_REMARKS: ""},
    )
    await hass.async_block_till_done()
    cfg = entry.options[CONF_ENTITIES]["device_tracker.car"]
    assert cfg[CONF_MODE] == MODE_MARKER
    assert cfg[CONF_COT_TYPE] == "a-f-G-E-V-C"

    # Remove it again.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "remove_entity"}
    )
    assert result["step_id"] == "remove_entity"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"entity_ids": ["device_tracker.car"]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options[CONF_ENTITIES] == {}


async def test_sensor_and_unload(hass: HomeAssistant, fake_tak_server):
    entry = await _setup_entry(hass, fake_tak_server)

    hass.states.async_set(
        "person.nick", "home", {"latitude": 1.0, "longitude": 2.0}
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_entity"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"entity_id": "person.nick", CONF_MODE: MODE_CONTACT}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_CALLSIGN: "NICK",
            CONF_TEAM_COLOR: "Cyan",
            CONF_TEAM_ROLE: "Team Member",
            CONF_REMARKS: "",
        },
    )
    await hass.async_block_till_done()
    await _wait_for_events(fake_tak_server)
    await hass.async_block_till_done()

    sensor = next(
        s for s in hass.states.async_all("sensor") if "last_report" in s.entity_id
    )
    assert sensor.state not in ("unknown", "unavailable")
    assert sensor.attributes["tracked_entities"] == ["person.nick"]
    assert sensor.attributes["events_sent"] >= 1
    assert sensor.attributes["last_error"] is None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
