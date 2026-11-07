"""TAK Server certificate enrollment with a username and password.

This is the same flow ATAK/iTAK/WinTAK use for "Enroll for client
certificate": the client authenticates to the server's certificate
enrollment API (HTTPS, port 8446 by default) with HTTP Basic auth, asks it
which subject fields to put in a certificate signing request, sends a CSR
with ``CN=<username>``, and gets back a client certificate signed by the
server's CA together with that CA chain. The resulting certificate is then
used for the normal TLS streaming connection (port 8089).
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone

import aiohttp
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_CLIENT_UID,
    CONF_ENROLLED_CA,
    CONF_ENROLLED_CERT,
    CONF_ENROLLED_KEY,
    CONF_ENROLLMENT_PORT,
    CONF_TLS_CA_FILE,
    CONF_TLS_VERIFY,
    DEFAULT_ENROLLMENT_PORT,
)
from .tak import relax_x509_strict, resolve_path

_LOGGER = logging.getLogger(__name__)

CONFIG_PATH = "/Marti/api/tls/config"
SIGN_PATH = "/Marti/api/tls/signClient/v2"
REQUEST_TIMEOUT = 30

# Subject fields TAK Server may ask for in /tls/config.
_NAME_OIDS = {
    "C": NameOID.COUNTRY_NAME,
    "ST": NameOID.STATE_OR_PROVINCE_NAME,
    "L": NameOID.LOCALITY_NAME,
    "O": NameOID.ORGANIZATION_NAME,
    "OU": NameOID.ORGANIZATIONAL_UNIT_NAME,
}


class EnrollmentError(Exception):
    """Enrollment failed for a reason other than bad credentials."""


class EnrollmentAuthError(EnrollmentError):
    """The server rejected the username or password."""


class EnrollmentConnectError(EnrollmentError):
    """The enrollment endpoint could not be reached."""


class EnrollmentCertificateError(EnrollmentError):
    """The enrollment endpoint's TLS certificate could not be verified."""


@dataclass
class EnrollmentResult:
    """PEM material obtained from a successful enrollment."""

    cert_pem: str
    key_pem: str
    ca_pem: str
    not_valid_after: datetime


def _pem_wrap(body: str, label: str = "CERTIFICATE") -> str:
    """TAK Server returns base64 bodies without PEM armor; add it if needed."""
    body = body.strip()
    if body.startswith("-----BEGIN"):
        return body + "\n"
    b64 = "".join(body.split())
    lines = [b64[i : i + 64] for i in range(0, len(b64), 64)]
    return f"-----BEGIN {label}-----\n" + "\n".join(lines) + f"\n-----END {label}-----\n"


def cert_expiry(cert_pem: str) -> datetime:
    """Return the notAfter time of a PEM certificate (UTC)."""
    cert = x509.load_pem_x509_certificate(cert_pem.encode())
    try:
        return cert.not_valid_after_utc
    except AttributeError:  # cryptography < 42
        return cert.not_valid_after.replace(tzinfo=timezone.utc)


def parse_name_entries(body: str) -> list[tuple[str, str]]:
    """Parse the CSR subject template from /tls/config.

    The document looks like::

        <ns2:certificateConfig ...>
          <nameEntries>
            <nameEntry name="O" value="TAK"/>
            <nameEntry name="OU" value="TAK"/>
          </nameEntries>
        </ns2:certificateConfig>
    """
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return []
    entries = []
    for elem in root.iter():
        if elem.tag.rsplit("}", 1)[-1] != "nameEntry":
            continue
        name, value = elem.get("name"), elem.get("value")
        if name and value:
            entries.append((name.strip(), value.strip()))
    return entries


def _generate_key_and_csr(
    username: str, name_entries: list[tuple[str, str]]
) -> tuple[str, str]:
    """Return (private key PEM, CSR base64 body without PEM armor)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    attrs = [
        x509.NameAttribute(_NAME_OIDS[name.upper()], value)
        for name, value in name_entries
        if name.upper() in _NAME_OIDS
    ]
    attrs.append(x509.NameAttribute(NameOID.COMMON_NAME, username))
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name(attrs))
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    # ATAK posts the CSR as its bare base64 body; every TAK server
    # implementation accepts that form.
    csr_body = "".join(
        line
        for line in csr.public_bytes(serialization.Encoding.PEM).decode().splitlines()
        if not line.startswith("-----")
    )
    return key_pem, csr_body


def parse_sign_response(body: str) -> tuple[str, list[str]]:
    """Return (client cert PEM, [CA cert PEMs]) from a signClient/v2 reply.

    TAK Server answers in JSON when asked (``{"signedCert": ..., "ca0": ...,
    "ca1": ...}``) and in XML otherwise (``<enrollment><signedCert>...
    </signedCert><ca>...</ca></enrollment>``)."""
    signed: str | None = None
    cas: list[str] = []
    text = body.strip()
    if text.startswith("{"):
        data = json.loads(text)
        signed = data.get("signedCert")
        cas = [
            data[key]
            for key in sorted(k for k in data if k.startswith("ca"))
            if isinstance(data[key], str) and data[key].strip()
        ]
    elif text.startswith("<"):
        root = ET.fromstring(text)
        for elem in root.iter():
            tag = elem.tag.rsplit("}", 1)[-1]
            if tag == "signedCert" and elem.text:
                signed = elem.text
            elif tag.startswith("ca") and elem.text and elem.text.strip():
                cas.append(elem.text)
    if not signed:
        raise EnrollmentError(
            "The TAK server did not return a signed certificate"
        )
    return _pem_wrap(signed), [_pem_wrap(ca) for ca in cas]


def build_enrollment_ssl_context(
    ca_file: str | None, verify: bool
) -> ssl.SSLContext:
    """TLS context for the enrollment endpoint (blocking — run in executor).

    Without a CA file the system trust store is used, which works when the
    enrollment port is fronted by a public certificate (e.g. Let's
    Encrypt). TAK Server's own certificate is issued for an internal name,
    so the hostname is never checked, mirroring ATAK."""
    context = ssl.create_default_context(cafile=ca_file or None)
    relax_x509_strict(context)
    context.check_hostname = False
    if not verify:
        context.verify_mode = ssl.CERT_NONE
    return context


async def async_enroll(
    hass: HomeAssistant,
    *,
    host: str,
    port: int,
    username: str,
    password: str,
    client_uid: str,
    ssl_context: ssl.SSLContext,
) -> EnrollmentResult:
    """Obtain a client certificate from TAK Server using credentials."""
    session = async_get_clientsession(hass)
    auth = aiohttp.BasicAuth(username, password)
    base = f"https://{host}:{port}"
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)

    async def _request(method: str, path: str, **kwargs) -> str:
        try:
            async with session.request(
                method,
                base + path,
                auth=auth,
                ssl=ssl_context,
                timeout=timeout,
                **kwargs,
            ) as resp:
                text = await resp.text()
                if resp.status in (401, 403):
                    raise EnrollmentAuthError(
                        f"TAK server rejected the username or password "
                        f"(HTTP {resp.status})"
                    )
                if resp.status >= 400:
                    raise EnrollmentError(
                        f"{path} returned HTTP {resp.status}: {text[:200]}"
                    )
                return text
        except aiohttp.ClientConnectorCertificateError as exc:
            raise EnrollmentCertificateError(
                f"Could not verify the enrollment server certificate: "
                f"{exc.certificate_error}. Provide the TAK server's CA file "
                "or turn off certificate verification."
            ) from exc
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise EnrollmentConnectError(
                f"Could not reach the TAK enrollment service at {base}: "
                f"{exc or type(exc).__name__}"
            ) from exc

    # The subject template is optional; older servers may not serve it.
    name_entries: list[tuple[str, str]] = []
    try:
        name_entries = parse_name_entries(await _request("GET", CONFIG_PATH))
    except EnrollmentAuthError:
        raise
    except EnrollmentError as exc:
        _LOGGER.debug("No CSR subject template from %s: %s", base, exc)

    key_pem, csr_body = await hass.async_add_executor_job(
        _generate_key_and_csr, username, name_entries
    )
    body = await _request(
        "POST",
        SIGN_PATH,
        params={"clientUid": client_uid, "version": "HA2TAK"},
        data=csr_body,
        headers={"Content-Type": "text/plain", "Accept": "application/json"},
    )
    try:
        cert_pem, ca_pems = parse_sign_response(body)
        cert = x509.load_pem_x509_certificate(cert_pem.encode())
    except (ValueError, ET.ParseError) as exc:
        raise EnrollmentError(
            f"Could not understand the TAK server's enrollment reply: {exc}"
        ) from exc

    key = serialization.load_pem_private_key(key_pem.encode(), password=None)
    if cert.public_key().public_numbers() != key.public_key().public_numbers():
        raise EnrollmentError(
            "The certificate returned by the TAK server does not match the "
            "submitted request"
        )

    result = EnrollmentResult(
        cert_pem=cert_pem,
        key_pem=key_pem,
        ca_pem="".join(ca_pems),
        not_valid_after=cert_expiry(cert_pem),
    )
    _LOGGER.info(
        "Enrolled with TAK server %s as %s; certificate valid until %s",
        host,
        username,
        result.not_valid_after.isoformat(),
    )
    return result


async def async_enroll_from_config(
    hass: HomeAssistant, data: dict
) -> dict[str, str]:
    """Enroll using the settings of a config entry; return the entry data
    keys holding the new certificate material.

    Raises the Enrollment* errors above, or TakConfigError for a bad CA
    file path."""
    ca_file = await hass.async_add_executor_job(
        resolve_path, hass.config.config_dir, data.get(CONF_TLS_CA_FILE) or ""
    )
    ssl_context = await hass.async_add_executor_job(
        build_enrollment_ssl_context, ca_file, data.get(CONF_TLS_VERIFY, True)
    )
    result = await async_enroll(
        hass,
        host=data[CONF_HOST],
        port=int(data.get(CONF_ENROLLMENT_PORT) or DEFAULT_ENROLLMENT_PORT),
        username=data[CONF_USERNAME],
        password=data[CONF_PASSWORD],
        client_uid=data[CONF_CLIENT_UID],
        ssl_context=ssl_context,
    )
    return {
        CONF_ENROLLED_CERT: result.cert_pem,
        CONF_ENROLLED_KEY: result.key_pem,
        CONF_ENROLLED_CA: result.ca_pem,
    }
