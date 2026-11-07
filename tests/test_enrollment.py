"""Username/password certificate enrollment: flows, renewal and reauth."""

from __future__ import annotations

import asyncio
import json
import xml.etree.ElementTree as ET
from datetime import timedelta

from cryptography import x509
from cryptography.x509.oid import NameOID
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.ha2tak.const import (
    AUTH_CERTIFICATES,
    AUTH_ENROLLMENT,
    CONF_AUTH_METHOD,
    CONF_CALLSIGN,
    CONF_CLIENT_UID,
    CONF_ENROLLED_CA,
    CONF_ENROLLED_CERT,
    CONF_ENROLLED_KEY,
    CONF_ENROLLMENT_PORT,
    CONF_ENTITIES,
    CONF_MODE,
    CONF_PROTOCOL,
    CONF_TLS_CA_FILE,
    CONF_TLS_CERT_FILE,
    CONF_TLS_CHECK_HOSTNAME,
    CONF_TLS_KEY_FILE,
    CONF_TLS_VERIFY,
    DOMAIN,
    MODE_CONTACT,
)
from custom_components.ha2tak.enrollment import (
    parse_name_entries,
    parse_sign_response,
)
from custom_components.ha2tak.tak import TakConnection

from .tak_pki import TLS_CONFIG_XML


async def _start_tls_flow(hass: HomeAssistant, tls_tak_server) -> dict:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_HOST: "127.0.0.1", CONF_PORT: tls_tak_server.port, CONF_PROTOCOL: "tls"},
    )
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["tls_enroll", "tls_certificates"]
    return result


def _enroll_input(enrollment_server, password: str = "s3cret") -> dict:
    return {
        CONF_USERNAME: "hauser",
        CONF_PASSWORD: password,
        CONF_ENROLLMENT_PORT: enrollment_server.port,
        CONF_TLS_CA_FILE: "tak_ca.pem",  # relative to the HA config folder
        CONF_TLS_VERIFY: True,
        CONF_TLS_CHECK_HOSTNAME: False,
    }


async def _wait_for(predicate, what: str) -> None:
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"Timed out waiting for {what}")


async def test_enroll_and_stream_over_mutual_tls(
    hass: HomeAssistant, enrollment_server, tls_tak_server
):
    result = await _start_tls_flow(hass, tls_tak_server)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "tls_enroll"}
    )
    assert result["step_id"] == "tls_enroll"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _enroll_input(enrollment_server)
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    await hass.async_block_till_done()
    entry = result["result"]
    assert entry.state is ConfigEntryState.LOADED

    data = entry.data
    assert data[CONF_AUTH_METHOD] == AUTH_ENROLLMENT
    assert data[CONF_USERNAME] == "hauser"
    assert "BEGIN PRIVATE KEY" in data[CONF_ENROLLED_KEY]
    assert "BEGIN CERTIFICATE" in data[CONF_ENROLLED_CA]

    # The CSR used the server's subject template plus CN=username.
    cert = x509.load_pem_x509_certificate(data[CONF_ENROLLED_CERT].encode())
    subject = {a.oid: a.value for a in cert.subject}
    assert subject[NameOID.COMMON_NAME] == "hauser"
    assert subject[NameOID.ORGANIZATION_NAME] == "TAK"
    assert subject[NameOID.ORGANIZATIONAL_UNIT_NAME] == "HA2TAK-Test"
    assert enrollment_server.sign_requests[0]["clientUid"] == data[CONF_CLIENT_UID]

    # The TLS server only accepts CA-signed client certs: reports reaching
    # it prove the enrolled certificate is what the connection presents.
    hass.states.async_set("person.nick", "home", {"latitude": 1.0, "longitude": 2.0})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_entity"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"entity_id": "person.nick", CONF_MODE: MODE_CONTACT}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_CALLSIGN: "NICK", "team_color": "Cyan", "team_role": "Team Member"},
    )
    await hass.async_block_till_done()
    assert entry.options[CONF_ENTITIES]

    await _wait_for(lambda: tls_tak_server.received, "a CoT event over TLS")
    root = ET.fromstring(b"".join(tls_tak_server.received).split(b"?>", 1)[1])
    assert root.get("uid") == "HA2TAK.person.nick"
    assert "hauser" in tls_tak_server.peer_cns


async def test_enroll_wrong_password(
    hass: HomeAssistant, enrollment_server, tls_tak_server
):
    result = await _start_tls_flow(hass, tls_tak_server)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "tls_enroll"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _enroll_input(enrollment_server, password="nope")
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    assert not enrollment_server.issued

    # Fixing the password on the same form succeeds.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _enroll_input(enrollment_server)
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_enroll_untrusted_server_certificate(
    hass: HomeAssistant, enrollment_server, tls_tak_server
):
    result = await _start_tls_flow(hass, tls_tak_server)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "tls_enroll"}
    )
    user_input = _enroll_input(enrollment_server)
    user_input[CONF_TLS_CA_FILE] = ""  # system trust store won't know our CA
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input
    )
    assert result["errors"] == {"base": "invalid_certificate"}


async def test_certificate_files_method(
    hass: HomeAssistant, tak_pki, tls_tak_server
):
    cert_pem, key_pem = tak_pki.issue_client("filecert", timedelta(days=30))
    (tak_pki.workdir / "client.pem").write_text(cert_pem)
    (tak_pki.workdir / "client.key").write_text(key_pem)

    result = await _start_tls_flow(hass, tls_tak_server)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "tls_certificates"}
    )
    assert result["step_id"] == "tls_certificates"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_TLS_CERT_FILE: "client.pem",
            CONF_TLS_KEY_FILE: "client.key",
            CONF_TLS_CA_FILE: "tak_ca.pem",
            CONF_TLS_VERIFY: True,
            CONF_TLS_CHECK_HOSTNAME: False,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    assert result["result"].data[CONF_AUTH_METHOD] == AUTH_CERTIFICATES
    await _wait_for(lambda: "filecert" in tls_tak_server.peer_cns, "TLS client")


def _enrolled_entry(tak_pki, enrollment_server, tls_tak_server, validity, password="s3cret"):
    cert_pem, key_pem = tak_pki.issue_client("hauser", validity)
    return MockConfigEntry(
        domain=DOMAIN,
        title="TAK server 127.0.0.1",
        unique_id=f"127.0.0.1:{tls_tak_server.port}",
        data={
            CONF_HOST: "127.0.0.1",
            CONF_PORT: tls_tak_server.port,
            CONF_PROTOCOL: "tls",
            CONF_CLIENT_UID: "HA2TAK-test",
            CONF_AUTH_METHOD: AUTH_ENROLLMENT,
            CONF_USERNAME: "hauser",
            CONF_PASSWORD: password,
            CONF_ENROLLMENT_PORT: enrollment_server.port,
            CONF_TLS_CA_FILE: "tak_ca.pem",
            CONF_TLS_VERIFY: True,
            CONF_TLS_CHECK_HOSTNAME: False,
            CONF_ENROLLED_CERT: cert_pem,
            CONF_ENROLLED_KEY: key_pem,
            CONF_ENROLLED_CA: "",
        },
    )


async def test_valid_certificate_is_not_renewed(
    hass: HomeAssistant, tak_pki, enrollment_server, tls_tak_server
):
    entry = _enrolled_entry(tak_pki, enrollment_server, tls_tak_server, timedelta(days=200))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert not enrollment_server.issued


async def test_expiring_certificate_is_renewed(
    hass: HomeAssistant, tak_pki, enrollment_server, tls_tak_server
):
    entry = _enrolled_entry(tak_pki, enrollment_server, tls_tak_server, timedelta(days=2))
    old_cert = entry.data[CONF_ENROLLED_CERT]
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert len(enrollment_server.issued) == 1
    assert entry.data[CONF_ENROLLED_CERT] != old_cert
    assert "BEGIN CERTIFICATE" in entry.data[CONF_ENROLLED_CA]


async def test_rejected_password_on_renewal_starts_reauth(
    hass: HomeAssistant, tak_pki, enrollment_server, tls_tak_server
):
    entry = _enrolled_entry(
        tak_pki, enrollment_server, tls_tak_server, timedelta(days=-1), password="old"
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR

    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1
    assert flows[0]["context"]["source"] == SOURCE_REAUTH
    result = await hass.config_entries.flow.async_configure(
        flows[0]["flow_id"], {CONF_USERNAME: "hauser", CONF_PASSWORD: "s3cret"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    await hass.async_block_till_done()
    assert entry.data[CONF_PASSWORD] == "s3cret"
    assert entry.state is ConfigEntryState.LOADED


async def test_reconfigure_switches_tls_method(
    hass: HomeAssistant, tak_pki, enrollment_server, tls_tak_server
):
    entry = _enrolled_entry(tak_pki, enrollment_server, tls_tak_server, timedelta(days=200))
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    cert_pem, key_pem = tak_pki.issue_client("filecert", timedelta(days=30))
    (tak_pki.workdir / "client.pem").write_text(cert_pem + key_pem)

    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_HOST: "127.0.0.1", CONF_PORT: tls_tak_server.port, CONF_PROTOCOL: "tls"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "tls_certificates"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_TLS_CERT_FILE: "client.pem",
            CONF_TLS_CA_FILE: "tak_ca.pem",
            CONF_TLS_VERIFY: True,
            CONF_TLS_CHECK_HOSTNAME: False,
        },
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    # Enrollment credentials and material are dropped with the method.
    assert entry.data[CONF_AUTH_METHOD] == AUTH_CERTIFICATES
    for key in (CONF_PASSWORD, CONF_USERNAME, CONF_ENROLLED_CERT, CONF_ENROLLED_KEY):
        assert key not in entry.data


async def test_enrolled_ca_chain_verifies_streaming_server(
    hass: HomeAssistant, tak_pki, tls_tak_server
):
    """Without a CA file, the CA chain from enrollment is the trust anchor."""
    cert_pem, key_pem = tak_pki.issue_client("hauser", timedelta(days=30))
    connection = TakConnection(
        hass,
        {
            CONF_HOST: "127.0.0.1",
            CONF_PORT: tls_tak_server.port,
            CONF_PROTOCOL: "tls",
            CONF_TLS_VERIFY: True,
            CONF_ENROLLED_CERT: cert_pem,
            CONF_ENROLLED_KEY: key_pem,
            CONF_ENROLLED_CA: tak_pki.ca_file.read_text(),
        },
    )
    await connection.async_connect()
    await connection.async_send([b"<event/>"])
    await connection.async_close()
    await _wait_for(lambda: tls_tak_server.received, "data over TLS")


def test_parse_name_entries():
    assert parse_name_entries(TLS_CONFIG_XML) == [("O", "TAK"), ("OU", "HA2TAK-Test")]
    assert parse_name_entries("not xml") == []


async def test_parse_sign_response_xml_and_armored(tak_pki):
    ca_pem = tak_pki.ca_file.read_text()
    body = ca_pem.split("-----")[2].strip()
    xml = f"<enrollment><signedCert>{body}</signedCert><ca>{body}</ca></enrollment>"
    signed, cas = parse_sign_response(xml)
    assert x509.load_pem_x509_certificate(signed.encode())
    assert len(cas) == 1
    signed, cas = parse_sign_response(json.dumps({"signedCert": ca_pem}))
    assert signed.startswith("-----BEGIN CERTIFICATE-----")
    assert cas == []
