"""A throwaway TAK-like PKI plus fake enrollment and TLS streaming servers."""

from __future__ import annotations

import asyncio
import base64
import json
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiohttp import web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

TLS_CONFIG_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<ns2:certificateConfig xmlns="http://bbn.com/marti/xml/config" '
    'xmlns:ns2="com.bbn.marti.config">'
    "<nameEntries>"
    '<nameEntry name="O" value="TAK"/>'
    '<nameEntry name="OU" value="HA2TAK-Test"/>'
    "</nameEntries>"
    "</ns2:certificateConfig>"
)


def _key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _pem(obj) -> bytes:
    if isinstance(obj, rsa.RSAPrivateKey):
        return obj.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    return obj.public_bytes(serialization.Encoding.PEM)


def _strip_armor(pem: bytes) -> str:
    return "".join(
        line for line in pem.decode().splitlines() if not line.startswith("-----")
    )


@dataclass
class TakPki:
    """A CA that issues the server certificate and signs client CSRs."""

    workdir: Path
    ca_key: rsa.RSAPrivateKey = field(default_factory=_key)
    client_validity: timedelta = timedelta(days=365)

    def __post_init__(self) -> None:
        now = datetime.now(timezone.utc)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test TAK CA")])
        self.ca_cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(self.ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
            .sign(self.ca_key, hashes.SHA256())
        )
        # Like a real TAK server: issued for an internal name, not the
        # address clients dial.
        server_key = _key()
        server_cert = self._issue(
            server_key.public_key(),
            x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "takserver")]),
            timedelta(days=365),
        )
        self.ca_file = self.workdir / "tak_ca.pem"
        self.ca_file.write_bytes(_pem(self.ca_cert))
        self.server_cert_file = self.workdir / "server.pem"
        self.server_cert_file.write_bytes(_pem(server_cert) + _pem(server_key))

    def _issue(self, public_key, subject: x509.Name, validity: timedelta):
        now = datetime.now(timezone.utc)
        return (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(self.ca_cert.subject)
            .public_key(public_key)
            .serial_number(x509.random_serial_number())
            .not_valid_before(min(now - timedelta(minutes=5), now + validity - timedelta(days=30)))
            .not_valid_after(now + validity)
            .sign(self.ca_key, hashes.SHA256())
        )

    def issue_client(self, cn: str, validity: timedelta) -> tuple[str, str]:
        """Return (cert PEM, key PEM) for a client certificate."""
        key = _key()
        cert = self._issue(
            key.public_key(),
            x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)]),
            validity,
        )
        return _pem(cert).decode(), _pem(key).decode()

    def sign_csr(self, csr_text: str) -> x509.Certificate:
        if not csr_text.startswith("-----BEGIN"):
            csr_text = (
                "-----BEGIN CERTIFICATE REQUEST-----\n"
                + csr_text
                + "\n-----END CERTIFICATE REQUEST-----\n"
            )
        csr = x509.load_pem_x509_csr(csr_text.encode())
        assert csr.is_signature_valid
        return self._issue(csr.public_key(), csr.subject, self.client_validity)

    def server_context(self, require_client_cert: bool) -> ssl.SSLContext:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.server_cert_file)
        if require_client_cert:
            ctx.verify_mode = ssl.CERT_REQUIRED
            ctx.load_verify_locations(self.ca_file)
        return ctx


class FakeEnrollmentServer:
    """TAK Server's /Marti/api/tls enrollment endpoints over HTTPS."""

    def __init__(self, pki: TakPki, username: str, password: str) -> None:
        self.pki = pki
        self.username = username
        self.password = password
        self.issued: list[x509.Certificate] = []
        self.sign_requests: list[dict] = []
        self.port = 0
        self._runner: web.AppRunner | None = None

    def _authorized(self, request: web.Request) -> bool:
        expected = base64.b64encode(
            f"{self.username}:{self.password}".encode()
        ).decode()
        return request.headers.get("Authorization") == f"Basic {expected}"

    async def _config(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.Response(status=401)
        return web.Response(text=TLS_CONFIG_XML, content_type="application/xml")

    async def _sign(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.Response(status=401)
        self.sign_requests.append(dict(request.query))
        cert = self.pki.sign_csr(await request.text())
        self.issued.append(cert)
        body = {
            "signedCert": _strip_armor(_pem(cert)),
            "ca0": _strip_armor(_pem(self.pki.ca_cert)),
        }
        return web.Response(text=json.dumps(body), content_type="application/json")

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/Marti/api/tls/config", self._config)
        app.router.add_post("/Marti/api/tls/signClient/v2", self._sign)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(
            self._runner,
            "127.0.0.1",
            0,
            ssl_context=self.pki.server_context(require_client_cert=False),
        )
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()


class FakeTlsTakServer:
    """TAK streaming input on TLS that insists on a CA-signed client cert."""

    def __init__(self, pki: TakPki) -> None:
        self.pki = pki
        self.received: list[bytes] = []
        self.peer_cns: list[str] = []
        self.port = 0
        self._server: asyncio.Server | None = None
        self._writers: list[asyncio.StreamWriter] = []

    async def _handle(self, reader, writer) -> None:
        self._writers.append(writer)
        peer = writer.get_extra_info("peercert") or {}
        for rdn in peer.get("subject", ()):
            for key, value in rdn:
                if key == "commonName":
                    self.peer_cns.append(value)
        while data := await reader.read(65536):
            self.received.append(data)
        writer.close()

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle,
            "127.0.0.1",
            0,
            ssl=self.pki.server_context(require_client_cert=True),
        )
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            for writer in self._writers:
                writer.close()
            await self._server.wait_closed()


