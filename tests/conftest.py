"""Shared fixtures for HA2TAK tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from .tak_pki import FakeEnrollmentServer, FakeTlsTakServer, TakPki


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Allow loading the ha2tak custom integration in tests."""
    yield


@pytest.fixture
def tak_pki(hass):
    """A TAK-like CA whose files live in the HA config folder."""
    return TakPki(Path(hass.config.config_dir))


@pytest.fixture
async def enrollment_server(tak_pki, socket_enabled):
    server = FakeEnrollmentServer(tak_pki, username="hauser", password="s3cret")
    await server.start()
    yield server
    await server.stop()


@pytest.fixture
async def tls_tak_server(tak_pki, socket_enabled):
    server = FakeTlsTakServer(tak_pki)
    await server.start()
    yield server
    await server.stop()


@pytest.fixture
async def fake_tak_server(socket_enabled):
    """A local TCP server standing in for a TAK server: captures what
    clients send and can push events to them (``await push(b"...")``),
    optionally sending ``initial`` events to every new client like TAK
    Server's latest-SA replay."""
    received: list[bytes] = []
    writers: list[asyncio.StreamWriter] = []
    initial: list[bytes] = []
    connected = asyncio.Event()

    async def _handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        writers.append(writer)
        for event in initial:
            writer.write(event)
        connected.set()
        while True:
            data = await reader.read(65536)
            if not data:
                break
            received.append(data)
        writer.close()

    async def push(data: bytes) -> None:
        await asyncio.wait_for(connected.wait(), 5)
        for writer in writers:
            if not writer.is_closing():
                writer.write(data)
                await writer.drain()

    async def drop_clients() -> None:
        connected.clear()
        for writer in list(writers):
            writer.close()
        writers.clear()

    server = await asyncio.start_server(_handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    yield SimpleNamespace(
        host="127.0.0.1",
        port=port,
        received=received,
        initial=initial,
        push=push,
        drop_clients=drop_clients,
        writers=writers,
    )
    server.close()
    # Drop any client connection still open so wait_closed cannot hang.
    for writer in writers:
        writer.close()
    await server.wait_closed()
