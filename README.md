# HA2TAK

A Home Assistant custom integration that reports the location of selected
Home Assistant entities — **people, device trackers and zones** — to a
**TAK server** (TAK Server, FreeTAKServer, taky, …) as standard
**Cursor-on-Target (CoT) 2.0** events. Your family, vehicles, pets or any
GPS-tracked entity shows up live in **ATAK, WinTAK and iTAK**.

It also works the other way around: pick **TAK callsigns** — teammates
running ATAK/iTAK/WinTAK, vehicles with TAK trackers — and their positions
become **GPS device trackers in Home Assistant**, usable in zones,
automations, maps and People.

The CoT generation and TAK delivery are derived from the
[XPloraTAK](https://github.com/ickynk/XPloraTAK) add-on, reworked as a
native integration with full UI configuration.

## Features

- **UI-only setup** — no YAML. Configure the TAK server connection, then
  pick entities from a menu.
- **TCP, TLS and UDP** delivery. For TLS, authenticate either with
  - a **username and password** — Home Assistant enrolls with the TAK
    server for a client certificate exactly like ATAK's *"Enroll for client
    certificate"*, and **renews it automatically** before it expires; or
  - **certificate files** — a CA/truststore file plus a PEM client
    certificate + key, or a **.p12 bundle** from a TAK Server data package
    (default password is usually `atakatak`).
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
- **Pull TAK callsigns into Home Assistant** as `device_tracker` entities,
  live over the same connection, with battery, team, role, speed, course,
  altitude and stale time. Pick callsigns from those recently seen on the
  server or type one.
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
2. Enter the server: **host**, **port** and **protocol**. TAK Server takes
   plain TCP on **8087** and TLS on **8089**; bytes sent with the wrong
   protocol are **silently discarded** by the server.
3. For **TLS**, choose how Home Assistant authenticates:

   **Username and password** (recommended when your TAK account allows
   certificate enrollment):

   | Field | Notes |
   |---|---|
   | Username / Password | Your TAK Server account. The username becomes the certificate's common name — the identity the server sees. |
   | Enrollment port | TAK Server's certificate enrollment API, normally **8446**. |
   | CA / truststore file | Optional. Your TAK server's CA (PEM), used to verify the enrollment server — your password is sent to it. If the enrollment port has a publicly trusted certificate (e.g. Let's Encrypt), leave empty. |
   | Verify server certificate | Turn off only if the enrollment port is self-signed and you have no CA file. |

   Home Assistant fetches the server's certificate template
   (`/Marti/api/tls/config`), generates a private key and signing request
   with `CN=<username>`, and sends it to `/Marti/api/tls/signClient/v2`
   with your credentials. The server returns a signed client certificate
   and its CA chain; the chain is used to verify the 8089 connection when
   no CA file is given. The certificate is renewed automatically when it
   is within 14 days of expiry (checked whenever the integration starts or
   reloads). If the server rejects the stored password during renewal,
   Home Assistant shows a **re-authenticate** prompt.

   The certificate, private key and password are stored in Home
   Assistant's config entry storage (`.storage/core.config_entries`),
   like other integrations' credentials.

   **Certificate files:**

   | Field | Notes |
   |---|---|
   | Client certificate / key | PEM files. |
   | .p12 bundle | Alternative to the PEM pair — the enrollment/data-package bundle from TAK Server. Password is usually `atakatak`. |
   | CA / truststore file | PEM file with your TAK server's CA. |

   Both methods also offer **Verify server certificate** (disable only for
   setups you trust) and **Verify server hostname** (off by default: TAK
   server certificates are usually issued for an internal name, so
   ATAK/iTAK skip hostname checks too). Certificate paths are resolved
   relative to the Home Assistant configuration folder (e.g.
   `ssl/tak_ca.pem` → `/config/ssl/tak_ca.pem`). The connection, including
   the TLS handshake with the client certificate, is tested before the
   entry is saved.

4. Open the integration's **Configure** menu to:
   - **Add an entity to report to TAK** — pick a `person`,
     `device_tracker` or `zone` entity and choose **Contact** or **Mission
     marker**, then fill in the call sign and the mode-specific fields.
   - **Edit / Remove reported entities.**
   - **Pull a TAK callsign into Home Assistant / Stop pulling TAK
     callsigns** — see [below](#pulling-tak-callsigns-into-home-assistant).
   - **Reporting settings** — reporting interval (default 60 s), stale
     time (default 300 s; use at least 2–3× the interval), and whether to
     also send a report immediately when an entity's location changes.

Changing the server connection later — including switching between
username/password and certificate files: use the entry's **Reconfigure**
option (⋮ menu on the integration entry). When reconfiguring
username/password, leave the password empty to keep the stored one.

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

## Pulling TAK callsigns into Home Assistant

The TCP/TLS connection is two-way: the TAK server streams the positions of
other users to it, just as it does to ATAK. Choose **Configure → Pull a TAK
callsign into Home Assistant**, then pick a callsign from those recently
seen on the server or type one, and optionally give it a name. A
`device_tracker` entity is created (e.g. `device_tracker.alpha_one`) and
updates every time that callsign reports.

- Callsigns are matched case-insensitively against the `<contact
  callsign>` of incoming CoT events, so any TAK client or marker with a
  callsign works.
- The state is `home`, `not_home` or a zone name, like any GPS tracker;
  `gps_accuracy` comes from the report's circular error, `battery_level`
  from its status.
- Attributes: `callsign`, `uid`, `cot_type`, `team`, `role`, `altitude`
  (m), `speed` (m/s), `course` (°), `remarks`, `last_seen`, `stale_at` and
  `stale` (true once the sender's stale time has passed without a new
  report).
- The last known position survives Home Assistant restarts and is replaced
  as soon as the callsign reports again. TAK Server also replays its latest
  known positions to a client when it connects.
- With nothing of its own to report, the integration sends the same
  keepalive ping ATAK uses each reporting interval, so a receive-only
  setup stays connected and reconnects after a drop.

Requirements and limits:

- **TCP or TLS only.** UDP connections can only send.
- The server only shares positions of users **in the same TAK groups** as
  this connection (for username/password sign-in, that's the account's
  groups; for certificates, the groups mapped to the certificate). If a
  callsign never shows up in the picker, check group membership first.
- The "Last report" sensor's `positions_received` attribute counts
  incoming positions — a quick way to see whether the server is sending
  anything at all.

## Service

```yaml
service: ha2tak.send_now
```

Sends reports for all configured entities to all configured TAK servers
immediately — handy in automations (e.g. push positions right after Home
Assistant starts or when someone leaves a zone).

## Troubleshooting

- **A pulled callsign never updates** — make sure the connection is TCP
  or TLS (not UDP) and that this connection shares a TAK group with that
  user. `positions_received` on the "Last report" sensor shows whether any
  positions arrive at all.
- **Nothing appears in ATAK, no errors** — protocol/port mismatch. TCP →
  8087, TLS → 8089; the server silently drops mismatched traffic.
- **"TAK server closed the connection right after our data"** — the
  client certificate wasn't accepted (wrong CA / not in the server
  truststore) or the account's groups don't allow the connection. Check
  the TAK server's messaging log.
- **"The TAK server rejected the username or password"** — check the
  account in TAK Server's user management; the account must be allowed to
  enroll for certificates.
- **"Could not reach the TAK server's enrollment service"** — the
  enrollment port (8446 by default) must be reachable from Home
  Assistant; it's often not exposed when only 8089 is port-forwarded.
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
