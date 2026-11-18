"""Pulling TAK callsigns into Home Assistant as device trackers."""

from __future__ import annotations

import asyncio

from pytest_homeassistant_custom_component.common import mock_restore_cache

from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, State
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er

from custom_components.ha2tak.const import (
    CONF_CALLSIGN,
    CONF_NAME,
    CONF_PROTOCOL,
    CONF_TAK_CALLSIGNS,
    DOMAIN,
)
from custom_components.ha2tak.cot import CotStreamParser, build_cot, build_ping, parse_cot

from .test_integration import _setup_entry


def _pli(callsign: str, lat: float, lon: float, **kwargs) -> bytes:
    """A position report shaped like ATAK's own PLI."""
    return build_cot(
        uid=kwargs.pop("uid", f"ANDROID-{callsign}"),
        callsign=callsign,
        lat=lat,
        lon=lon,
        contact_presence=True,
        **kwargs,
    )


async def _wait_for(predicate, what: str) -> None:
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"Timed out waiting for {what}")


async def _add_callsign(hass, entry, callsign: str, name: str | None = None):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert "add_callsign" in result["menu_options"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_callsign"}
    )
    assert result["step_id"] == "add_callsign"
    user_input = {CONF_CALLSIGN: callsign}
    if name:
        user_input[CONF_NAME] = name
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input
    )
    await hass.async_block_till_done()
    return result


def _tracker_entity_id(hass, entry) -> str:
    registry = er.async_get(hass)
    ids = [
        e.entity_id
        for e in er.async_entries_for_config_entry(registry, entry.entry_id)
        if e.domain == "device_tracker"
    ]
    assert len(ids) == 1, ids
    return ids[0]


def test_stream_parser_handles_split_and_back_to_back_events():
    one = _pli("A", 1.0, 2.0)
    two = _pli("B", 3.0, 4.0).replace(
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', b""
    )
    stream = one + b"\n" + two + build_ping("x")
    parser = CotStreamParser()
    events = []
    for i in range(0, len(stream), 7):  # arbitrary TCP segmentation
        events += parser.feed(stream[i : i + 7])
    assert len(events) == 3
    assert [r.callsign for r in map(parse_cot, events[:2])] == ["A", "B"]
    assert parse_cot(events[2]) is None  # ping is not a position


def test_parse_cot_fields_and_rejects():
    report = parse_cot(
        _pli(
            "ALPHA-1",
            51.5,
            -0.12,
            accuracy_m=8.0,
            altitude_m=35.0,
            course_deg=90.0,
            speed_ms=1.2,
            battery=77,
            remarks="on patrol",
            team_color="Red",
            team_role="Medic",
        )
    )
    assert report is not None
    assert (report.callsign, report.lat, report.lon) == ("ALPHA-1", 51.5, -0.12)
    assert (report.ce, report.hae, report.course, report.speed) == (8.0, 35.0, 90.0, 1.2)
    assert (report.battery, report.team, report.role) == (77, "Red", "Medic")
    assert report.remarks == "on patrol"
    assert report.stale is not None and report.time is not None

    unknown = parse_cot(_pli("X", 1.0, 2.0))
    assert unknown.ce is None and unknown.hae is None  # 9999999 = unknown
    assert parse_cot(b"<event><broken") is None
    assert parse_cot(b'<event type="a-f-G"><point lat="1" lon="2"/><detail/></event>') is None
    assert parse_cot(_pli("X", 123.0, 2.0)) is None  # out-of-range latitude


async def test_pull_callsign_into_device_tracker(hass: HomeAssistant, fake_tak_server):
    entry = await _setup_entry(hass, fake_tak_server)

    # Traffic seen before anything is tracked feeds the callsign picker.
    await fake_tak_server.push(_pli("ALPHA-1", 10.0, 20.0, cot_type="a-f-G-U-C"))
    tracker = entry.runtime_data.tracker
    await _wait_for(lambda: tracker.seen, "callsign to be seen")
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_callsign"}
    )
    picker = result["data_schema"].schema[CONF_CALLSIGN].config
    assert [o["value"] for o in picker["options"]] == ["ALPHA-1"]
    assert picker["custom_value"] is True
    hass.config_entries.options.async_abort(result["flow_id"])

    # The server replays the latest position to the reconnected client.
    fake_tak_server.initial.append(_pli("ALPHA-1", 10.0, 20.0, battery=55))
    result = await _add_callsign(hass, entry, "alpha-1", name="Alpha One")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_TAK_CALLSIGNS] == {
        "alpha-1": {CONF_CALLSIGN: "alpha-1", CONF_NAME: "Alpha One"}
    }

    entity_id = _tracker_entity_id(hass, entry)
    assert entity_id == "device_tracker.alpha_one"
    await _wait_for(
        lambda: hass.states.get(entity_id).attributes.get("latitude") == 10.0,
        "replayed position",
    )
    state = hass.states.get(entity_id)
    assert state.state == "not_home"
    assert state.attributes["battery_level"] == 55
    assert state.attributes["source_type"] == "gps"

    # Live update, matched case-insensitively.
    await fake_tak_server.push(
        _pli(
            "ALPHA-1",
            hass.config.latitude,
            hass.config.longitude,
            accuracy_m=5.0,
            team_color="Red",
            team_role="Team Lead",
            speed_ms=2.5,
        )
    )
    await _wait_for(lambda: hass.states.get(entity_id).state == "home", "home")
    attrs = hass.states.get(entity_id).attributes
    assert attrs["gps_accuracy"] == 5.0
    assert attrs["team"] == "Red"
    assert attrs["role"] == "Team Lead"
    assert attrs["speed"] == 2.5
    assert attrs["uid"] == "ANDROID-ALPHA-1"
    assert attrs["stale"] is False
    assert attrs["battery_level"] == 55  # kept from the earlier report

    # Other callsigns don't touch it.
    await fake_tak_server.push(_pli("BRAVO-2", 1.0, 1.0))
    await _wait_for(
        lambda: "bravo-2" in entry.runtime_data.tracker.seen, "bravo seen"
    )
    assert hass.states.get(entity_id).state == "home"

    # Removing the callsign removes the entity.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "remove_callsign"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"callsigns": ["alpha-1"]}
    )
    await hass.async_block_till_done()
    assert entry.options[CONF_TAK_CALLSIGNS] == {}
    assert er.async_get(hass).async_get(entity_id) is None


async def test_add_callsign_errors(hass: HomeAssistant, fake_tak_server):
    entry = await _setup_entry(hass, fake_tak_server)
    await _add_callsign(hass, entry, "Charlie")
    result = await _add_callsign(hass, entry, " CHARLIE ")
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_CALLSIGN: "callsign_already_tracked"}


async def test_udp_cannot_receive(hass: HomeAssistant, socket_enabled):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_HOST: "127.0.0.1", CONF_PORT: 6969, CONF_PROTOCOL: "udp"},
    )
    entry = result["result"]
    await hass.async_block_till_done()
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_callsign"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "udp_cannot_receive"


async def test_last_position_restored_after_restart(
    hass: HomeAssistant, fake_tak_server
):
    entry = await _setup_entry(hass, fake_tak_server)
    await _add_callsign(hass, entry, "DELTA")
    entity_id = _tracker_entity_id(hass, entry)
    assert hass.states.get(entity_id).state == "unknown"  # never seen yet

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    mock_restore_cache(
        hass,
        [
            State(
                entity_id,
                "not_home",
                {
                    "latitude": 40.0,
                    "longitude": -70.0,
                    "gps_accuracy": 12,
                    "callsign": "DELTA",
                    "team": "Blue",
                },
            )
        ],
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    state = hass.states.get(entity_id)
    assert state.attributes["latitude"] == 40.0
    assert state.attributes["team"] == "Blue"


async def test_receive_only_reconnects_after_drop(hass: HomeAssistant, fake_tak_server):
    entry = await _setup_entry(hass, fake_tak_server)
    await _add_callsign(hass, entry, "ECHO")
    entity_id = _tracker_entity_id(hass, entry)
    runtime = entry.runtime_data

    # An idle interval sends a keepalive ping instead of nothing.
    await runtime.reporter.async_report()
    await _wait_for(
        lambda: b"t-x-c-t" in b"".join(fake_tak_server.received), "keepalive ping"
    )

    await fake_tak_server.drop_clients()
    await _wait_for(lambda: not runtime.connection.connected, "disconnect noticed")

    # The next interval reconnects; positions flow again.
    await runtime.reporter.async_report()
    assert runtime.connection.connected
    await fake_tak_server.push(_pli("ECHO", 5.0, 6.0))
    await _wait_for(
        lambda: hass.states.get(entity_id).attributes.get("latitude") == 5.0,
        "position after reconnect",
    )
