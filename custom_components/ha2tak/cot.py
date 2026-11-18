"""Cursor-on-Target (CoT) 2.0 event generation and parsing.

Builds standard CoT <event> XML from a Home Assistant entity's location,
and parses the events a TAK server streams back. Two outgoing shapes are
produced:

* **contact** — shaped like a TAK client's own position report (PLI): it
  carries ``<takv>``, a server-routable ``<contact endpoint>``,
  ``<uid Droid>`` and ``<__group>``. That is what makes the callsign appear
  in ATAK/WinTAK's contacts list instead of being just an anonymous map
  marker.
* **marker** — a plain map marker of a user-selected CoT type (hostile
  ground, waypoint, sensor point of interest, ...).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from xml.sax.saxutils import escape, quoteattr

COT_TIME_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"
UNKNOWN = "9999999.0"

TAKV_PLATFORM = "HA2TAK"
TAKV_DEVICE = "Home Assistant"
TAKV_VERSION = "1.2.0"


def _cot_time(value: datetime) -> str:
    """Format a datetime the way TAK clients expect (millisecond Zulu)."""
    return value.astimezone(timezone.utc).strftime(COT_TIME_FMT)[:-4] + "Z"


def build_cot(
    *,
    uid: str,
    callsign: str,
    lat: float,
    lon: float,
    cot_type: str = "a-f-G-U-C",
    contact_presence: bool = True,
    accuracy_m: Optional[float] = None,
    altitude_m: Optional[float] = None,
    course_deg: Optional[float] = None,
    speed_ms: Optional[float] = None,
    battery: Optional[int] = None,
    fix_time: Optional[datetime] = None,
    stale_seconds: int = 300,
    remarks: str = "",
    team_color: str = "Cyan",
    team_role: str = "Team Member",
) -> bytes:
    """Return a CoT <event> document as UTF-8 bytes."""
    now = datetime.now(timezone.utc)
    start = fix_time or now
    stale = now + timedelta(seconds=stale_seconds)

    ce = f"{accuracy_m:.1f}" if accuracy_m and accuracy_m > 0 else UNKNOWN
    hae = f"{altitude_m:.1f}" if altitude_m is not None else UNKNOWN

    if contact_presence:
        detail_parts = [
            f'<takv device="{TAKV_DEVICE}" platform="{TAKV_PLATFORM}" '
            f'os="linux" version="{TAKV_VERSION}"/>',
            f'<contact callsign={quoteattr(callsign)} endpoint="*:-1:stcp"/>',
            f"<uid Droid={quoteattr(callsign)}/>",
            '<precisionlocation altsrc="GPS" geopointsrc="GPS"/>',
            f"<__group name={quoteattr(team_color)} role={quoteattr(team_role)}/>",
        ]
    else:
        detail_parts = [f"<contact callsign={quoteattr(callsign)}/>"]
    if course_deg is not None or speed_ms is not None:
        course = f"{course_deg:.1f}" if course_deg is not None else "0.0"
        speed = f"{speed_ms:.2f}" if speed_ms is not None else "0.00"
        detail_parts.append(f'<track course="{course}" speed="{speed}"/>')
    if battery is not None:
        detail_parts.append(f'<status battery="{int(battery)}"/>')
    if remarks:
        detail_parts.append(f"<remarks>{escape(remarks)}</remarks>")
    detail = "".join(detail_parts)

    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f"<event version=\"2.0\" uid={quoteattr(uid)} type={quoteattr(cot_type)} "
        f'how="m-g" time="{_cot_time(now)}" '
        f'start="{_cot_time(start)}" '
        f'stale="{_cot_time(stale)}">'
        f'<point lat="{lat:.7f}" lon="{lon:.7f}" hae="{hae}" '
        f'ce="{ce}" le="{UNKNOWN}"/>'
        f"<detail>{detail}</detail>"
        "</event>"
    )
    return xml.encode("utf-8")


def build_ping(uid: str) -> bytes:
    """A TAK keepalive (``t-x-c-t``), as ATAK sends on idle connections."""
    now = datetime.now(timezone.utc)
    stale = now + timedelta(seconds=60)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<event version="2.0" uid={quoteattr(uid + "-ping")} type="t-x-c-t" '
        f'how="h-g-i-g-o" time="{_cot_time(now)}" start="{_cot_time(now)}" '
        f'stale="{_cot_time(stale)}">'
        f'<point lat="0.0" lon="0.0" hae="0.0" ce="{UNKNOWN}" le="{UNKNOWN}"/>'
        "<detail/></event>"
    ).encode("utf-8")


class CotStreamParser:
    """Split a TAK XML stream into individual ``<event>`` documents.

    TAK servers write events back to back on the connection, each with or
    without an ``<?xml?>`` declaration, and TCP may split them anywhere."""

    MAX_BUFFER = 1_000_000

    def __init__(self) -> None:
        self._buf = b""

    def feed(self, data: bytes) -> list[bytes]:
        self._buf += data
        events: list[bytes] = []
        while True:
            start = self._buf.find(b"<event")
            if start < 0:
                # Keep a tail in case "<event" itself was split.
                self._buf = self._buf[-6:]
                break
            end = self._buf.find(b"</event>", start)
            if end < 0:
                self._buf = self._buf[start:]
                break
            end += len(b"</event>")
            events.append(self._buf[start:end])
            self._buf = self._buf[end:]
        if len(self._buf) > self.MAX_BUFFER:
            # A runaway/garbage stream; drop it rather than grow forever.
            self._buf = b""
        return events


@dataclass
class CotReport:
    """The position-relevant contents of one received CoT event."""

    uid: str
    cot_type: str
    callsign: str
    lat: float
    lon: float
    hae: Optional[float] = None
    ce: Optional[float] = None
    time: Optional[datetime] = None
    stale: Optional[datetime] = None
    battery: Optional[int] = None
    course: Optional[float] = None
    speed: Optional[float] = None
    team: Optional[str] = None
    role: Optional[str] = None
    remarks: Optional[str] = None


def _known(value: Optional[str]) -> Optional[float]:
    """A float CoT attribute, or None for missing/"unknown" (9999999)."""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if number >= 9999999.0 or number != number:  # unknown sentinel / NaN
        return None
    return number


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def parse_cot(event: bytes) -> Optional[CotReport]:
    """Parse a received CoT event into a CotReport.

    Returns None for anything that isn't a position with a callsign:
    pings and other ``t-`` tasking messages, malformed XML, events without
    coordinates."""
    try:
        root = ET.fromstring(event)
    except ET.ParseError:
        return None
    if root.tag != "event":
        return None
    cot_type = root.get("type") or ""
    if cot_type.startswith("t-"):
        return None
    point = root.find("point")
    detail = root.find("detail")
    contact = detail.find("contact") if detail is not None else None
    callsign = contact.get("callsign") if contact is not None else None
    if point is None or not callsign:
        return None
    lat, lon = _known(point.get("lat")), _known(point.get("lon"))
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None

    report = CotReport(
        uid=root.get("uid") or "",
        cot_type=cot_type,
        callsign=callsign,
        lat=lat,
        lon=lon,
        hae=_known(point.get("hae")),
        ce=_known(point.get("ce")),
        time=_parse_time(root.get("time")),
        stale=_parse_time(root.get("stale")),
    )
    if detail is not None:
        status = detail.find("status")
        if status is not None and (battery := _known(status.get("battery"))) is not None:
            report.battery = max(0, min(100, int(battery)))
        track = detail.find("track")
        if track is not None:
            report.course = _known(track.get("course"))
            report.speed = _known(track.get("speed"))
        group = detail.find("__group")
        if group is not None:
            report.team = group.get("name")
            report.role = group.get("role")
        remarks = detail.find("remarks")
        if remarks is not None and remarks.text:
            report.remarks = remarks.text.strip()
    return report
