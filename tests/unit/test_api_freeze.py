"""Unit tests for ``POST /api/sessions/{id}/freeze``."""

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


@pytest.mark.asyncio
async def test_freeze_whole_session_creates_version(
    client: TestClient, store: InMemoryStore
) -> None:
    session = await store.create_session(title="s")
    for i in range(3):
        await store.append_event(session_id=session.session_id, kind="turn", payload={"i": i})

    resp = client.post(
        f"/api/sessions/{session.session_id}/freeze",
        json={"label": "snapshot"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["session_id"] == str(session.session_id)
    assert body["start_seq"] == 0
    assert body["end_seq"] == 2
    assert body["turn_count"] == 3
    assert body["label"] == "snapshot"


@pytest.mark.asyncio
async def test_freeze_range_returns_ranged_version(
    client: TestClient, store: InMemoryStore
) -> None:
    session = await store.create_session(title="s")
    for i in range(5):
        await store.append_event(session_id=session.session_id, kind="turn", payload={"i": i})

    resp = client.post(
        f"/api/sessions/{session.session_id}/freeze",
        json={"start_seq": 1, "end_seq": 3},
    )
    body = resp.json()
    assert body["start_seq"] == 1
    assert body["end_seq"] == 3
    assert body["turn_count"] == 3


@pytest.mark.asyncio
async def test_freeze_empty_session_returns_409(client: TestClient, store: InMemoryStore) -> None:
    session = await store.create_session(title="s")
    resp = client.post(f"/api/sessions/{session.session_id}/freeze", json={})
    assert resp.status_code == 409


def test_freeze_unknown_session_returns_404(client: TestClient) -> None:
    resp = client.post(f"/api/sessions/{uuid4()}/freeze", json={})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_freeze_appears_in_list_versions(client: TestClient, store: InMemoryStore) -> None:
    session = await store.create_session(title="s")
    await store.append_event(session_id=session.session_id, kind="turn", payload={"x": 1})
    freeze_resp = client.post(f"/api/sessions/{session.session_id}/freeze", json={})
    version_id = freeze_resp.json()["version_id"]

    list_resp = client.get(f"/api/sessions/{session.session_id}/versions")
    assert [v["version_id"] for v in list_resp.json()] == [version_id]
