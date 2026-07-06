"""Unit tests for CoT event generation."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from custom_components.ha2tak.cot import build_cot


def test_contact_event_shape():
    event = build_cot(
        uid="HA2TAK.person.nick",
        callsign="NICK",
        lat=51.5007325,
        lon=-0.1272003,
        contact_presence=True,
        accuracy_m=12.0,
        battery=88,
        fix_time=datetime(2026, 7, 6, 12, 0, 0, tzinfo=timezone.utc),
        stale_seconds=300,
        remarks="from HA",
        team_color="Red",
        team_role="Medic",
    )
    root = ET.fromstring(event)
    assert root.tag == "event"
    assert root.get("uid") == "HA2TAK.person.nick"
    assert root.get("type") == "a-f-G-U-C"
    point = root.find("point")
    assert point.get("lat") == "51.5007325"
    assert point.get("lon") == "-0.1272003"
    assert point.get("ce") == "12.0"
    detail = root.find("detail")
    assert detail.find("takv").get("platform") == "HA2TAK"
    contact = detail.find("contact")
    assert contact.get("callsign") == "NICK"
    assert contact.get("endpoint") == "*:-1:stcp"
    group = detail.find("__group")
    assert group.get("name") == "Red"
    assert group.get("role") == "Medic"
    assert detail.find("status").get("battery") == "88"
    assert detail.find("remarks").text == "from HA"
    assert root.get("start") == "2026-07-06T12:00:00.000Z"


def test_marker_event_shape():
    event = build_cot(
        uid="HA2TAK.zone.cache",
        callsign="Supply cache",
        lat=1.0,
        lon=2.0,
        cot_type="a-h-G",
        contact_presence=False,
    )
    root = ET.fromstring(event)
    assert root.get("type") == "a-h-G"
    detail = root.find("detail")
    # Markers must not carry PLI/presence elements.
    assert detail.find("takv") is None
    assert detail.find("__group") is None
    assert detail.find("contact").get("endpoint") is None
    assert detail.find("contact").get("callsign") == "Supply cache"
    # Unknown accuracy/altitude use the CoT "unknown" sentinel.
    point = root.find("point")
    assert point.get("ce") == "9999999.0"
    assert point.get("hae") == "9999999.0"


def test_track_and_escaping():
    event = build_cot(
        uid="u",
        callsign='Say "hi" & <bye>',
        lat=0.0,
        lon=0.0,
        contact_presence=False,
        course_deg=180.0,
        speed_ms=1.5,
        altitude_m=100.0,
        remarks="a & b <c>",
    )
    root = ET.fromstring(event)  # would raise if escaping were broken
    detail = root.find("detail")
    track = detail.find("track")
    assert track.get("course") == "180.0"
    assert track.get("speed") == "1.50"
    assert root.find("point").get("hae") == "100.0"
    assert detail.find("remarks").text == "a & b <c>"
