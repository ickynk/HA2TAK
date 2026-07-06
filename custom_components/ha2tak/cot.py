"""Cursor-on-Target (CoT) 2.0 event generation.

Builds standard CoT <event> XML from a Home Assistant entity's location.
Two shapes are produced:

* **contact** — shaped like a TAK client's own position report (PLI): it
  carries ``<takv>``, a server-routable ``<contact endpoint>``,
  ``<uid Droid>`` and ``<__group>``. That is what makes the callsign appear
  in ATAK/WinTAK's contacts list instead of being just an anonymous map
  marker.
* **marker** — a plain map marker of a user-selected CoT type (hostile
  ground, waypoint, sensor point of interest, ...).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional
from xml.sax.saxutils import escape, quoteattr

COT_TIME_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"
UNKNOWN = "9999999.0"

TAKV_PLATFORM = "HA2TAK"
TAKV_DEVICE = "Home Assistant"
TAKV_VERSION = "1.0.0"


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
