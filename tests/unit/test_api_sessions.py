"""Tests for ``/api/sessions`` REST endpoints (CRUD + fork)."""

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from stratoclave_atelier.config import AtelierConfig
from stratoclave_atelier.db import InMemoryStore
from stratoclave_atelier.server import create_app


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture
def client(stub_config: AtelierConfig, store: InMemoryStore) -> Iterator[TestClient]:
    app = create_app(stub_config, store=store)
    with TestClient(app) as client:
        yield client


def test_create_root_session(client: TestClient) -> None:
    resp = client.post("/api/sessions", json={"title": "root"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["title"] == "root"
    assert body["parent_session_id"] is None
    assert body["status"] == "active"


def test_create_session_in_unknown_group_returns_404(client: TestClient) -> None:
    resp = client.post("/api/sessions", json={"title": "x", "group_id": str(uuid4())})
    assert resp.status_code == 404


def test_list_and_get_session(client: TestClient) -> None:
    created = client.post("/api/sessions", json={"title": "t"}).json()
    listed = client.get("/api/sessions").json()
    assert [s["session_id"] for s in listed] == [created["session_id"]]
    detail = client.get(f"/api/sessions/{created['session_id']}").json()
    assert detail["session_id"] == created["session_id"]


def test_get_unknown_session_returns_404(client: TestClient) -> None:
    resp = client.get(f"/api/sessions/{uuid4()}")
    assert resp.status_code == 404


async def _seed_parent_with_version(store: InMemoryStore) -> tuple[str, str]:
    parent = await store.create_session(title="parent")
    version = await store.create_version(
        session_id=parent.session_id,
        blob_sha="a" * 64,
        blob_path="x.jsonl",
        start_seq=0,
        end_seq=5,
        byte_size=10,
    )
    return str(parent.session_id), str(version.version_id)


@pytest.mark.asyncio
async def test_fork_session_succeeds(client: TestClient, store: InMemoryStore) -> None:
    parent_id, version_id = await _seed_parent_with_version(store)
    resp = client.post(
        f"/api/sessions/{parent_id}/fork",
        json={
            "title": "child",
            "parent_version_id": version_id,
            "fork_seq": 3,
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["parent_session_id"] == parent_id
    assert body["parent_version_id"] == version_id
    assert body["fork_seq"] == 3


@pytest.mark.asyncio
async def test_fork_session_with_out_of_range_seq_returns_409(
    client: TestClient, store: InMemoryStore
) -> None:
    parent_id, version_id = await _seed_parent_with_version(store)
    resp = client.post(
        f"/api/sessions/{parent_id}/fork",
        json={
            "title": "child",
            "parent_version_id": version_id,
            "fork_seq": 99,
        },
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_list_versions_for_session(client: TestClient, store: InMemoryStore) -> None:
    parent_id, version_id = await _seed_parent_with_version(store)
    resp = client.get(f"/api/sessions/{parent_id}/versions")
    assert resp.status_code == 200
    body = resp.json()
    assert [v["version_id"] for v in body] == [version_id]


def test_list_versions_for_unknown_session_returns_404(client: TestClient) -> None:
    resp = client.get(f"/api/sessions/{uuid4()}/versions")
    assert resp.status_code == 404
