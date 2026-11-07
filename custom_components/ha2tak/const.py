"""Constants for the HA2TAK integration."""

from __future__ import annotations

DOMAIN = "ha2tak"

# Connection (config entry data)
CONF_PROTOCOL = "protocol"
CONF_TLS_CA_FILE = "tls_ca_file"
CONF_TLS_CERT_FILE = "tls_cert_file"
CONF_TLS_KEY_FILE = "tls_key_file"
CONF_TLS_P12_FILE = "tls_p12_file"
CONF_TLS_P12_PASSWORD = "tls_p12_password"
CONF_TLS_VERIFY = "tls_verify"
CONF_TLS_CHECK_HOSTNAME = "tls_check_hostname"

# TLS authentication method
CONF_AUTH_METHOD = "tls_auth"
AUTH_CERTIFICATES = "certificates"
AUTH_ENROLLMENT = "enrollment"

# Username/password certificate enrollment
CONF_ENROLLMENT_PORT = "enrollment_port"
CONF_CLIENT_UID = "client_uid"
CONF_ENROLLED_CERT = "enrolled_cert"
CONF_ENROLLED_KEY = "enrolled_key"
CONF_ENROLLED_CA = "enrolled_ca"
DEFAULT_ENROLLMENT_PORT = 8446
# Re-enroll automatically when the client certificate expires within this.
RENEW_BEFORE_DAYS = 14

PROTOCOL_TCP = "tcp"
PROTOCOL_TLS = "tls"
PROTOCOL_UDP = "udp"
PROTOCOLS = [PROTOCOL_TCP, PROTOCOL_TLS, PROTOCOL_UDP]

DEFAULT_PORT = 8087
DEFAULT_TLS_PORT = 8089

# Reporting (config entry options)
CONF_INTERVAL = "report_interval"
CONF_STALE = "stale_seconds"
CONF_SEND_ON_UPDATE = "send_on_update"
CONF_ENTITIES = "entities"

DEFAULT_INTERVAL = 60
DEFAULT_STALE = 300
MIN_INTERVAL = 5
MAX_INTERVAL = 86400

# Per-entity configuration
CONF_MODE = "mode"
CONF_CALLSIGN = "callsign"
CONF_COT_TYPE = "cot_type"
CONF_TEAM_COLOR = "team_color"
CONF_TEAM_ROLE = "team_role"
CONF_REMARKS = "remarks"

MODE_CONTACT = "contact"
MODE_MARKER = "marker"
MODES = [MODE_CONTACT, MODE_MARKER]

# CoT type used for contact-mode (PLI-style) reports, the same type ATAK
# clients use for their own position.
CONTACT_COT_TYPE = "a-f-G-U-C"

# Common mission markers a user can pick for marker-mode entities.
# value = CoT type, label shown in the UI.
MARKER_TYPES: dict[str, str] = {
    "b-m-p-s-m": "Marker (generic point)",
    "a-u-G": "Unknown ground",
    "a-f-G": "Friendly ground",
    "a-n-G": "Neutral ground",
    "a-h-G": "Hostile ground",
    "a-f-G-E-V-C": "Friendly vehicle",
    "a-n-G-E-V-C": "Neutral vehicle",
    "a-h-G-E-V-C": "Hostile vehicle",
    "a-f-A": "Friendly aircraft",
    "a-h-A": "Hostile aircraft",
    "a-f-S": "Friendly vessel",
    "a-h-S": "Hostile vessel",
    "b-m-p-w": "Waypoint",
    "b-m-p-c": "Contact point",
    "b-m-p-s-p-i": "Sensor point of interest (SPI)",
}
DEFAULT_MARKER_TYPE = "b-m-p-s-m"

# ATAK team (group) colors and roles for contact-mode entities.
TEAM_COLORS = [
    "White",
    "Yellow",
    "Orange",
    "Magenta",
    "Red",
    "Maroon",
    "Purple",
    "Dark Blue",
    "Blue",
    "Cyan",
    "Teal",
    "Green",
    "Dark Green",
    "Brown",
]
DEFAULT_TEAM_COLOR = "Cyan"

TEAM_ROLES = [
    "Team Member",
    "Team Lead",
    "HQ",
    "Sniper",
    "Medic",
    "Forward Observer",
    "RTO",
    "K9",
]
DEFAULT_TEAM_ROLE = "Team Member"

# Entity domains offered in the entity picker.
TRACKABLE_DOMAINS = ["person", "device_tracker", "zone"]

SERVICE_SEND_NOW = "send_now"

ATTR_TRACKED_ENTITIES = "tracked_entities"
ATTR_EVENTS_SENT = "events_sent"
ATTR_LAST_ERROR = "last_error"
