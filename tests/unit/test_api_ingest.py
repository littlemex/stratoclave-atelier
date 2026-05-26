"""Unit tests for the WebSocket ingest endpoint."""

from __future__ import annotations

import json
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


@pytest.mark.asyncio
async def test_ingest_appends_turns_and_acks(client: TestClient, store: InMemoryStore) -> None:
    session = await store.create_session(title="s")

    with client.websocket_connect(f"/api/sessions/{session.session_id}/ingest") as ws:
        ws.send_text(json.dumps({"role": "user", "content": "hi"}))
        ack1 = ws.receive_json()
        ws.send_text(json.dumps({"role": "assistant", "content": "hello"}))
        ack2 = ws.receive_json()

    assert ack1["type"] == "ack"
    assert ack1["seq"] == 0
    assert ack2["seq"] == 1

    events = await store.list_events(session.session_id)
    assert [e.payload["content"] for e in events] == ["hi", "hello"]


def test_ingest_rejects_unknown_session(client: TestClient) -> None:
    from starlette.websockets import WebSocketDisconnect

    with (
        pytest.raises(WebSocketDisconnect) as exc_info,
        client.websocket_connect(f"/api/sessions/{uuid4()}/ingest") as ws,
    ):
        ws.receive_text()
    assert exc_info.value.code == 4404


@pytest.mark.asyncio
async def test_ingest_rejects_invalid_json(client: TestClient, store: InMemoryStore) -> None:
    session = await store.create_session(title="s")
    with client.websocket_connect(f"/api/sessions/{session.session_id}/ingest") as ws:
        ws.send_text("not json")
        err = ws.receive_json()
        assert err["type"] == "error"
        assert err["code"] == "invalid_json"


@pytest.mark.asyncio
async def test_ingest_rejects_non_object_payload(client: TestClient, store: InMemoryStore) -> None:
    session = await store.create_session(title="s")
    with client.websocket_connect(f"/api/sessions/{session.session_id}/ingest") as ws:
        ws.send_text(json.dumps([1, 2, 3]))
        err = ws.receive_json()
        assert err["type"] == "error"
        assert err["code"] == "invalid_payload"
