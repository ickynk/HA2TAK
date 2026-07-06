"""Asyncio delivery of CoT events to a TAK server over TCP, TLS or UDP.

The TCP/TLS connection is held open between reports, like a real TAK
client: TAK Server treats streaming inputs as sessions (a contact stays
"online" while its connection lives), and reading from the socket is the
only way to learn whether the server actually accepted us — with TLS 1.3 a
rejected client certificate surfaces as a disconnect *after* a successful
handshake, invisible to a write-and-close sender.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import ssl
import tempfile
from typing import Any

from homeassistant.core import HomeAssistant

from .const import (
    CONF_TLS_CA_FILE,
    CONF_TLS_CERT_FILE,
    CONF_TLS_CHECK_HOSTNAME,
    CONF_TLS_KEY_FILE,
    CONF_TLS_P12_FILE,
    CONF_TLS_P12_PASSWORD,
    CONF_TLS_VERIFY,
    CONF_PROTOCOL,
    PROTOCOL_TCP,
    PROTOCOL_TLS,
    PROTOCOL_UDP,
)
from homeassistant.const import CONF_HOST, CONF_PORT

_LOGGER = logging.getLogger(__name__)

CONNECT_TIMEOUT = 15
DRAIN_TIMEOUT = 1.0


class TakConfigError(Exception):
    """The TLS/certificate configuration is unusable (bad path, bad p12)."""


def _resolve_path(config_dir: str, path: str) -> str:
    """Resolve a certificate path relative to the HA configuration folder."""
    path = path.strip()
    if not path:
        return ""
    if not os.path.isabs(path):
        path = os.path.join(config_dir, path)
    if not os.path.isfile(path):
        raise TakConfigError(
            f"Certificate file not found: {path}. Paths are relative to the "
            "Home Assistant configuration folder unless absolute."
        )
    return path


def _extract_p12(p12_file: str, password: str) -> tuple[bytes, bytes | None]:
    """Extract client cert+key (and CA chain) from a PKCS#12 bundle, as
    handed out by TAK Server enrollment / data packages."""
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
        pkcs12,
    )

    with open(p12_file, "rb") as fh:
        blob = fh.read()
    try:
        key, cert, extra_certs = pkcs12.load_key_and_certificates(
            blob, password.encode() or None
        )
    except ValueError as exc:
        raise TakConfigError(
            f"Could not read {p12_file}: {exc}. Check the p12 password "
            "(TAK Server bundles often use 'atakatak')."
        ) from exc
    if key is None or cert is None:
        raise TakConfigError(
            f"{p12_file} does not contain a client key + certificate pair"
        )
    client_pem = cert.public_bytes(Encoding.PEM) + key.private_bytes(
        Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
    )
    ca_pem = None
    if extra_certs:
        ca_pem = b"".join(ca.public_bytes(Encoding.PEM) for ca in extra_certs)
    return client_pem, ca_pem


def build_ssl_context(data: dict[str, Any], config_dir: str) -> ssl.SSLContext:
    """Build the TLS context for a TAK server connection (blocking I/O —
    run in an executor)."""
    ca_file = _resolve_path(config_dir, data.get(CONF_TLS_CA_FILE) or "")
    cert_file = _resolve_path(config_dir, data.get(CONF_TLS_CERT_FILE) or "")
    key_file = _resolve_path(config_dir, data.get(CONF_TLS_KEY_FILE) or "")
    p12_file = _resolve_path(config_dir, data.get(CONF_TLS_P12_FILE) or "")

    client_pem = ca_pem = None
    if p12_file:
        client_pem, ca_pem = _extract_p12(
            p12_file, data.get(CONF_TLS_P12_PASSWORD) or ""
        )

    context = ssl.create_default_context(cafile=ca_file or None)
    if ca_pem and not ca_file:
        context.load_verify_locations(cadata=ca_pem.decode())

    if not data.get(CONF_TLS_VERIFY, True):
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    elif not data.get(CONF_TLS_CHECK_HOSTNAME, False):
        # TAK Server certificates are minted by its own CA for an internal
        # name ("takserver", a LAN IP), which never matches the public
        # hostname clients dial. ATAK/iTAK therefore verify the CA chain but
        # not the hostname — default to the same behavior.
        context.check_hostname = False

    if client_pem:
        # load_cert_chain only accepts file paths; the temp file can be
        # removed as soon as the context has read it.
        tmp = tempfile.NamedTemporaryFile(suffix=".pem", delete=False)
        try:
            tmp.write(client_pem)
            tmp.close()
            context.load_cert_chain(tmp.name)
        finally:
            os.unlink(tmp.name)
    elif cert_file:
        try:
            context.load_cert_chain(cert_file, key_file or None)
        except (ssl.SSLError, OSError) as exc:
            raise TakConfigError(
                f"Could not load client certificate {cert_file}: {exc}"
            ) from exc
    return context


class TakConnection:
    """A persistent CoT delivery channel to one TAK server."""

    def __init__(self, hass: HomeAssistant, data: dict[str, Any]) -> None:
        self._hass = hass
        self._data = dict(data)
        self.host: str = data[CONF_HOST]
        self.port: int = int(data[CONF_PORT])
        self.protocol: str = data.get(CONF_PROTOCOL) or PROTOCOL_TCP
        self._ssl_context: ssl.SSLContext | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        # The classic misconfiguration: TAK Server listens for plain TCP on
        # 8087 and TLS (client certs required) on 8089. Bytes sent with the
        # wrong protocol are silently discarded server-side, so warn loudly.
        if self.protocol == PROTOCOL_TCP and self.port == 8089:
            _LOGGER.warning(
                "Port 8089 is normally the TLS input but protocol is 'tcp' - "
                "the server will silently drop these events. Use protocol "
                "'tls' (with certificates) or port 8087."
            )
        elif self.protocol == PROTOCOL_TLS and self.port == 8087:
            _LOGGER.warning(
                "Port 8087 is normally the plain-TCP input but protocol is "
                "'tls' - use protocol 'tcp' or port 8089."
            )

    @property
    def connected(self) -> bool:
        return self.protocol == PROTOCOL_UDP or self._writer is not None

    async def async_connect(self) -> None:
        """Open the connection (TLS handshake included). No-op for UDP."""
        async with self._lock:
            await self._connect()

    async def _connect(self) -> None:
        if self.protocol == PROTOCOL_UDP or self._writer is not None:
            return
        ssl_context = None
        if self.protocol == PROTOCOL_TLS:
            if self._ssl_context is None:
                self._ssl_context = await self._hass.async_add_executor_job(
                    build_ssl_context, self._data, self._hass.config.config_dir
                )
            ssl_context = self._ssl_context
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port, ssl=ssl_context),
            CONNECT_TIMEOUT,
        )
        _LOGGER.info(
            "Connected to TAK server %s:%d (%s), holding connection open",
            self.host,
            self.port,
            self.protocol,
        )

    async def async_send(self, events: list[bytes]) -> None:
        """Deliver a batch of CoT events, reconnecting once if needed."""
        if not events:
            return
        async with self._lock:
            if self.protocol == PROTOCOL_UDP:
                await self._hass.async_add_executor_job(self._send_udp, events)
                return
            payload = b"".join(events)
            last_err: OSError | None = None
            for attempt in (1, 2):
                try:
                    await self._connect()
                    assert self._writer is not None
                    self._writer.write(payload)
                    await self._writer.drain()
                    await self._check_alive()
                    _LOGGER.debug(
                        "Sent %d CoT event(s) to %s:%d",
                        len(events),
                        self.host,
                        self.port,
                    )
                    return
                except (OSError, asyncio.TimeoutError) as exc:
                    last_err = exc if isinstance(exc, OSError) else OSError(str(exc))
                    await self._close()
                    if attempt == 1:
                        _LOGGER.debug(
                            "TAK connection lost (%s), reconnecting", exc
                        )
            assert last_err is not None
            raise last_err

    def _send_udp(self, events: list[bytes]) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            for event in events:
                sock.sendto(event, (self.host, self.port))

    async def _check_alive(self) -> None:
        """Read whatever the server pushed at us (other clients' SA traffic).

        This keeps the receive buffer empty on a long-lived connection and,
        crucially, detects the server hanging up on us — which after a
        successful TLS 1.3 handshake almost always means the client
        certificate was not accepted."""
        assert self._reader is not None
        try:
            while True:
                data = await asyncio.wait_for(
                    self._reader.read(65536), DRAIN_TIMEOUT
                )
                if not data:
                    raise OSError(
                        "TAK server closed the connection right after our "
                        "data. This usually means the client certificate was "
                        "not accepted (wrong CA / not in the server "
                        "truststore) or the account's groups don't allow the "
                        "connection - check the TAK server's messaging log."
                    )
                _LOGGER.debug(
                    "Received %d bytes from TAK server (connection accepted "
                    "and healthy)",
                    len(data),
                )
        except asyncio.TimeoutError:
            pass  # nothing pushed to us right now — connection is alive

    async def async_close(self) -> None:
        async with self._lock:
            await self._close()

    async def _close(self) -> None:
        if self._writer is not None:
            writer = self._writer
            self._reader = None
            self._writer = None
            try:
                writer.close()
                await writer.wait_closed()
            except (OSError, asyncio.TimeoutError):
                pass
