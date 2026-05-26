"""Unit tests for the SSE replay endpoint."""

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from stratoclave_atelier.blobs import InMemoryBlobStore
from stratoclave_atelier.config import AtelierConfig
from stratoclave_atelier.db import InMemoryStore
from stratoclave_atelier.server import create_app


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture
def blob_store() -> InMemoryBlobStore:
    return InMemoryBlobStore()


@pytest.fixture
def client(
    stub_config: AtelierConfig,
    store: InMemoryStore,
    blob_store: InMemoryBlobStore,
) -> Iterator[TestClient]:
    app = create_app(stub_config, store=store, blob_store=blob_store)
    with TestClient(app) as client:
        yield client


def _parse_sse(text: str) -> list[dict[str, str]]:
    """Parse a multi-frame SSE response body."""

    frames: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in text.splitlines():
        if line == "":
            if current:
                frames.append(current)
                current = {}
            continue
        if ":" in line:
            field, _, value = line.partition(": ")
            current[field] = value
    if current:
        frames.append(current)
    return frames


@pytest.mark.asyncio
async def test_replay_streams_all_events(client: TestClient, store: InMemoryStore) -> None:
    session = await store.create_session(title="s")
    for i in range(3):
        await store.append_event(session_id=session.session_id, kind="turn", payload={"i": i})

    resp = client.get(f"/api/sessions/{session.session_id}/events")
    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]

    frames = _parse_sse(resp.text)
    assert len(frames) == 3
    assert [f["id"] for f in frames] == ["0", "1", "2"]
    assert all(f["event"] == "turn" for f in frames)


@pytest.mark.asyncio
async def test_replay_respects_from_seq(client: TestClient, store: InMemoryStore) -> None:
    session = await store.create_session(title="s")
    for i in range(5):
        await store.append_event(session_id=session.session_id, kind="turn", payload={"i": i})

    resp = client.get(f"/api/sessions/{session.session_id}/events", params={"from_seq": 3})
    frames = _parse_sse(resp.text)
    assert [f["id"] for f in frames] == ["3", "4"]


def test_replay_unknown_session_returns_404(client: TestClient) -> None:
    resp = client.get(f"/api/sessions/{uuid4()}/events")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_replay_empty_session_returns_empty_stream(
    client: TestClient, store: InMemoryStore
) -> None:
    session = await store.create_session(title="s")
    resp = client.get(f"/api/sessions/{session.session_id}/events")
    assert resp.status_code == 200
    assert resp.text == ""
