# HA2TAK

A Home Assistant custom integration that reports the location of selected
Home Assistant entities — **people, device trackers and zones** — to a
**TAK server** (TAK Server, FreeTAKServer, taky, …) as standard
**Cursor-on-Target (CoT) 2.0** events. Your family, vehicles, pets or any
GPS-tracked entity shows up live in **ATAK, WinTAK and iTAK**.

The CoT generation and TAK delivery are derived from the
[XPloraTAK](https://github.com/ickynk/XPloraTAK) add-on, reworked as a
native integration with full UI configuration.

## Features

- **UI-only setup** — no YAML. Configure the TAK server connection, then
  pick entities from a menu.
- **TCP, TLS and UDP** delivery. TLS supports a CA/truststore file, a PEM
  client certificate + key, or a **.p12 bundle** straight from TAK Server
  enrollment / data packages (default password is usually `atakatak`).
- **Persistent connection**, like a real TAK client — contacts stay
  "online" on the server, and a rejected client certificate is detected
  instead of silently dropped.
- Per entity, choose how TAK sees it:
  - **Contact** — appears in the ATAK/WinTAK **contacts list** like a team
    member, with a configurable **call sign**, **team color** and **team
    role**.
  - **Mission marker** — appears as a map symbol of a selectable type:
    friendly/neutral/hostile/unknown ground, vehicles, aircraft, vessels,
    generic marker, waypoint, contact point, or sensor point of interest
    (SPI).
- Optional free-text **remarks** per entity, sent with every report.
- Configurable **reporting interval** and **stale time**, plus an optional
  mode that also reports immediately when an entity's location updates.
- Battery level, GPS accuracy, altitude, course and speed are included in
  the CoT when the entity provides them.
- A **Last report** sensor per server (with events-sent and last-error
  attributes) and a `ha2tak.send_now` service for automations.
- Multiple TAK servers: add the integration once per server.

## Installation

### HACS (recommended)

1. HACS → ⋮ → **Custom repositories** → add
   `https://github.com/ickynk/HA2TAK` as type **Integration**.
2. Install **HA2TAK** and restart Home Assistant.

### Manual

Copy `custom_components/ha2tak` into your Home Assistant `config/custom_components/`
folder and restart Home Assistant.

## Setup

1. **Settings → Devices & services → Add integration → HA2TAK.**
2. Enter your TAK server connection:

   | Field | Notes |
   |---|---|
   | Host / Port | TAK Server: plain TCP on **8087**, TLS on **8089**. Bytes sent with the wrong protocol are **silently discarded** by the server. |
   | Protocol | `tcp`, `tls` or `udp` |
   | CA / truststore file | PEM file with your TAK server's CA (TLS). |
   | Client certificate / key | PEM files (TLS with client auth). |
   | .p12 bundle | Alternative to the PEM pair — the enrollment/data-package bundle from TAK Server. Password is usually `atakatak`. |
   | Verify server certificate | Disable only for self-signed setups you trust. |
   | Verify server hostname | Off by default: TAK server certificates are usually issued for an internal name, so ATAK/iTAK skip hostname checks too. |

   Certificate paths are resolved relative to the Home Assistant
   configuration folder (e.g. `ssl/tak_ca.pem` → `/config/ssl/tak_ca.pem`).
   The connection (including the TLS handshake) is tested before the entry
   is created.

3. Open the integration's **Configure** menu to:
   - **Add an entity to report to TAK** — pick a `person`,
     `device_tracker` or `zone` entity and choose **Contact** or **Mission
     marker**, then fill in the call sign and the mode-specific fields.
   - **Edit / Remove reported entities.**
   - **Reporting settings** — reporting interval (default 60 s), stale
     time (default 300 s; use at least 2–3× the interval), and whether to
     also send a report immediately when an entity's location changes.

Changing the server connection later: use the entry's **Reconfigure**
option (⋮ menu on the integration entry).

## How entities are reported

- Coordinates come from the entity's `latitude`/`longitude` attributes.
  Router-based device trackers that only report a zone name fall back to
  that zone's coordinates; `not_home` without GPS attributes is skipped.
- **Contacts** are sent as `a-f-G-U-C` PLI events carrying `<takv>`,
  `<contact endpoint>`, `<uid Droid>` and `<__group>` — exactly the shape
  of a real TAK client's position report, which is what makes the call
  sign appear in the contacts list.
- **Markers** are plain CoT events of the selected type with the call sign
  as the marker label.
- Every report includes battery (`<status battery>`), GPS accuracy
  (`ce`), altitude (`hae`) and course/speed (`<track>`) when available.
- Reports are re-sent every interval even if the position hasn't changed,
  so markers never go stale on the TAK side while Home Assistant is up.

## Service

```yaml
service: ha2tak.send_now
```

Sends reports for all configured entities to all configured TAK servers
immediately — handy in automations (e.g. push positions right after Home
Assistant starts or when someone leaves a zone).

## Troubleshooting

- **Nothing appears in ATAK, no errors** — protocol/port mismatch. TCP →
  8087, TLS → 8089; the server silently drops mismatched traffic.
- **"TAK server closed the connection right after our data"** — the
  client certificate wasn't accepted (wrong CA / not in the server
  truststore) or the account's groups don't allow the connection. Check
  the TAK server's messaging log.
- **Certificate verification failed** — point the CA file at your TAK
  server's truststore PEM. Hostname verification is off by default; only
  enable it if your server certificate really matches its hostname.
- Enable debug logging:

  ```yaml
  logger:
    logs:
      custom_components.ha2tak: debug
  ```

## Disclaimer

Not affiliated with the TAK Product Center. Use in accordance with your
TAK server operator's policies.
