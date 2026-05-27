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


def test_patch_session_renames_title(client: TestClient) -> None:
    created = client.post("/api/sessions", json={"title": "auto"}).json()
    resp = client.patch(
        f"/api/sessions/{created['session_id']}",
        json={"title": "  hand-curated  "},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "hand-curated"
    again = client.get(f"/api/sessions/{created['session_id']}").json()
    assert again["title"] == "hand-curated"


def test_patch_session_rejects_empty_title(client: TestClient) -> None:
    created = client.post("/api/sessions", json={"title": "auto"}).json()
    resp = client.patch(
        f"/api/sessions/{created['session_id']}",
        json={"title": "   "},
    )
    # pydantic min_length=1 sees the raw whitespace string as length 3, so
    # the request passes schema validation; the store-side strip then
    # surfaces the empty title as a 409 conflict.
    assert resp.status_code == 409


def test_patch_unknown_session_returns_404(client: TestClient) -> None:
    resp = client.patch(f"/api/sessions/{uuid4()}", json={"title": "x"})
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


def test_append_turn_via_http_round_trips(client: TestClient) -> None:
    """``POST /api/sessions/{id}/turns`` mirrors the WS ingest path."""

    created = client.post("/api/sessions", json={"title": "t"}).json()
    resp = client.post(
        f"/api/sessions/{created['session_id']}/turns",
        json={"role": "user", "content": "hello"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["kind"] == "turn"
    assert body["payload"] == {"kind": "turn", "role": "user", "content": "hello"}
    assert body["seq"] == 0


def test_append_turn_to_unknown_session_returns_404(client: TestClient) -> None:
    resp = client.post(
        f"/api/sessions/{uuid4()}/turns",
        json={"role": "user", "content": "hello"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_append_turn_to_frozen_session_returns_409(
    client: TestClient, store: InMemoryStore
) -> None:
    session = await store.create_session(title="frozen-one")
    await store.update_session_status(session.session_id, "frozen")
    resp = client.post(
        f"/api/sessions/{session.session_id}/turns",
        json={"role": "user", "content": "hello"},
    )
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Stage H: per-session backend selection
# ---------------------------------------------------------------------------


@pytest.fixture
def multi_backend_client(store: InMemoryStore) -> Iterator[TestClient]:
    cfg = AtelierConfig.from_env(
        {
            "ATELIER_DATABASE_URL": "postgresql+asyncpg://a:b@localhost:5432/c",
            "ATELIER_AGENT_BACKENDS_ALLOWED": "claude_code,kiro_code,mock",
            "ATELIER_AGENT_CWD": "/tmp/wk",
            "ATELIER_AGENT_BACKEND": "claude_code",
        }
    )
    app = create_app(cfg, store=store)
    with TestClient(app) as client:
        yield client


def test_create_session_persists_agent_backend(multi_backend_client: TestClient) -> None:
    resp = multi_backend_client.post(
        "/api/sessions",
        json={"title": "kiro-session", "agent_backend": "kiro_code"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["agent_backend"] == "kiro_code"
    detail = multi_backend_client.get(f"/api/sessions/{body['session_id']}").json()
    assert detail["agent_backend"] == "kiro_code"


def test_create_session_without_backend_keeps_none(multi_backend_client: TestClient) -> None:
    resp = multi_backend_client.post("/api/sessions", json={"title": "default"})
    assert resp.status_code == 201
    assert resp.json()["agent_backend"] is None


def test_create_session_with_disallowed_backend_returns_409(
    multi_backend_client: TestClient,
) -> None:
    resp = multi_backend_client.post(
        "/api/sessions",
        json={"title": "bad", "agent_backend": "wizard"},
    )
    assert resp.status_code == 409


def test_create_session_when_no_backends_configured_rejects(
    client: TestClient,
) -> None:
    """The default `client` fixture has no backend configured."""

    resp = client.post(
        "/api/sessions",
        json={"title": "x", "agent_backend": "claude_code"},
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_fork_inherits_parent_backend_when_unspecified(
    multi_backend_client: TestClient, store: InMemoryStore
) -> None:
    parent = await store.create_session(title="parent", agent_backend="kiro_code")
    version = await store.create_version(
        session_id=parent.session_id,
        blob_sha="b" * 64,
        blob_path="x.jsonl",
        start_seq=0,
        end_seq=5,
        byte_size=10,
    )
    resp = multi_backend_client.post(
        f"/api/sessions/{parent.session_id}/fork",
        json={
            "title": "child",
            "parent_version_id": str(version.version_id),
            "fork_seq": 3,
        },
    )
    assert resp.status_code == 201
    assert resp.json()["agent_backend"] == "kiro_code"


@pytest.mark.asyncio
async def test_fork_overrides_parent_backend(
    multi_backend_client: TestClient, store: InMemoryStore
) -> None:
    parent = await store.create_session(title="parent", agent_backend="claude_code")
    version = await store.create_version(
        session_id=parent.session_id,
        blob_sha="c" * 64,
        blob_path="x.jsonl",
        start_seq=0,
        end_seq=5,
        byte_size=10,
    )
    resp = multi_backend_client.post(
        f"/api/sessions/{parent.session_id}/fork",
        json={
            "title": "child",
            "parent_version_id": str(version.version_id),
            "fork_seq": 3,
            "agent_backend": "mock",
        },
    )
    assert resp.status_code == 201
    assert resp.json()["agent_backend"] == "mock"
