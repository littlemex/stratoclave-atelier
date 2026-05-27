"""Tests for ``POST /api/sessions/{id}/branch`` (Stage J orchestrator)."""

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from stratoclave_atelier.auto_namer import AutoNamer
from stratoclave_atelier.blobs import InMemoryBlobStore
from stratoclave_atelier.config import AtelierConfig
from stratoclave_atelier.core.types import Event, Session
from stratoclave_atelier.db import InMemoryStore
from stratoclave_atelier.server import create_app


class _StubAutoNamer:
    """AutoNamer stub that returns a scripted title.

    Set ``raise_exc`` to simulate a Loom failure so the orchestrator
    falls through to the deterministic Noop suffix.
    """

    enabled: bool = True

    def __init__(
        self, *, title: str = "stub-title", raise_exc: BaseException | None = None
    ) -> None:
        self._title = title
        self._raise = raise_exc
        self.calls: list[tuple[Session, list[Event]]] = []

    async def name_branch(self, *, parent: Session, recent_events: list[Event]) -> str:
        self.calls.append((parent, list(recent_events)))
        if self._raise is not None:
            raise self._raise
        return self._title


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture
def blob_store() -> InMemoryBlobStore:
    return InMemoryBlobStore()


@pytest.fixture
def stub_namer() -> _StubAutoNamer:
    return _StubAutoNamer(title="API design questions")


@pytest.fixture
def client(
    stub_config: AtelierConfig,
    store: InMemoryStore,
    blob_store: InMemoryBlobStore,
    stub_namer: AutoNamer,
) -> Iterator[TestClient]:
    app = create_app(
        stub_config,
        store=store,
        blob_store=blob_store,
        auto_namer=stub_namer,
    )
    with TestClient(app) as client:
        yield client


@pytest.mark.asyncio
async def test_branch_freezes_and_forks(
    client: TestClient,
    store: InMemoryStore,
    stub_namer: _StubAutoNamer,
) -> None:
    parent = await store.create_session(title="parent")
    for i in range(3):
        await store.append_event(
            session_id=parent.session_id,
            kind="turn",
            payload={"kind": "turn", "role": "user", "content": f"hi-{i}"},
        )

    resp = client.post(f"/api/sessions/{parent.session_id}/branch", json={})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["auto_named"] is True
    assert body["child"]["title"] == "API design questions"
    assert body["child"]["parent_session_id"] == str(parent.session_id)
    assert body["child"]["parent_version_id"] == body["parent_version"]["version_id"]
    assert body["child"]["fork_seq"] == 2  # last seq frozen
    assert body["parent_version"]["start_seq"] == 0
    assert body["parent_version"]["end_seq"] == 2

    # AutoNamer received the parent + recent events
    assert len(stub_namer.calls) == 1
    parent_seen, events_seen = stub_namer.calls[0]
    assert parent_seen.session_id == parent.session_id
    assert len(events_seen) == 3


@pytest.mark.asyncio
async def test_branch_with_explicit_title_skips_auto_namer(
    client: TestClient,
    store: InMemoryStore,
    stub_namer: _StubAutoNamer,
) -> None:
    parent = await store.create_session(title="parent")
    await store.append_event(
        session_id=parent.session_id,
        kind="turn",
        payload={"kind": "turn", "role": "user", "content": "hi"},
    )

    resp = client.post(
        f"/api/sessions/{parent.session_id}/branch",
        json={"title": "manual override"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["child"]["title"] == "manual override"
    assert body["auto_named"] is False
    assert stub_namer.calls == []  # never called when caller pins title


@pytest.mark.asyncio
async def test_branch_with_range_uses_supplied_seqs(
    client: TestClient, store: InMemoryStore
) -> None:
    parent = await store.create_session(title="parent")
    for i in range(5):
        await store.append_event(
            session_id=parent.session_id,
            kind="turn",
            payload={"kind": "turn", "role": "user", "content": f"t-{i}"},
        )

    resp = client.post(
        f"/api/sessions/{parent.session_id}/branch",
        json={"start_seq": 1, "end_seq": 3, "title": "ranged"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["parent_version"]["start_seq"] == 1
    assert body["parent_version"]["end_seq"] == 3
    assert body["child"]["fork_seq"] == 3


@pytest.mark.asyncio
async def test_branch_falls_back_to_noop_on_namer_failure(
    stub_config: AtelierConfig,
    store: InMemoryStore,
    blob_store: InMemoryBlobStore,
) -> None:
    failing = _StubAutoNamer(raise_exc=RuntimeError("loom blew up"))
    app = create_app(stub_config, store=store, blob_store=blob_store, auto_namer=failing)
    parent = await store.create_session(title="atelier-main")
    await store.append_event(
        session_id=parent.session_id,
        kind="turn",
        payload={"kind": "turn", "role": "user", "content": "hi"},
    )
    with TestClient(app) as client:
        resp = client.post(f"/api/sessions/{parent.session_id}/branch", json={})
    assert resp.status_code == 201
    body = resp.json()
    # Fallback: parent.title-<4 hex>
    assert body["child"]["title"].startswith("atelier-main-")
    assert len(body["child"]["title"]) == len("atelier-main-") + 4
    assert body["auto_named"] is False


def test_branch_unknown_session_returns_404(client: TestClient) -> None:
    resp = client.post(f"/api/sessions/{uuid4()}/branch", json={})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_branch_empty_session_returns_409(client: TestClient, store: InMemoryStore) -> None:
    parent = await store.create_session(title="empty")
    resp = client.post(f"/api/sessions/{parent.session_id}/branch", json={})
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_branch_inherits_parent_backend(
    store: InMemoryStore,
    blob_store: InMemoryBlobStore,
    stub_namer: _StubAutoNamer,
) -> None:
    cfg = AtelierConfig.from_env(
        {
            "ATELIER_DATABASE_URL": "postgresql+asyncpg://a:b@localhost:5432/c",
            "ATELIER_AGENT_BACKENDS_ALLOWED": "claude_code,kiro_code",
            "ATELIER_AGENT_CWD": "/tmp/wk",
            "ATELIER_AGENT_BACKEND": "claude_code",
        }
    )
    parent = await store.create_session(title="p", agent_backend="kiro_code")
    await store.append_event(
        session_id=parent.session_id,
        kind="turn",
        payload={"kind": "turn", "role": "user", "content": "hi"},
    )
    app = create_app(cfg, store=store, blob_store=blob_store, auto_namer=stub_namer)
    with TestClient(app) as client:
        resp = client.post(f"/api/sessions/{parent.session_id}/branch", json={})
    assert resp.status_code == 201
    assert resp.json()["child"]["agent_backend"] == "kiro_code"


@pytest.mark.asyncio
async def test_branch_with_explicit_backend_override(
    store: InMemoryStore,
    blob_store: InMemoryBlobStore,
    stub_namer: _StubAutoNamer,
) -> None:
    cfg = AtelierConfig.from_env(
        {
            "ATELIER_DATABASE_URL": "postgresql+asyncpg://a:b@localhost:5432/c",
            "ATELIER_AGENT_BACKENDS_ALLOWED": "claude_code,kiro_code,mock",
            "ATELIER_AGENT_CWD": "/tmp/wk",
            "ATELIER_AGENT_BACKEND": "claude_code",
        }
    )
    parent = await store.create_session(title="p", agent_backend="claude_code")
    await store.append_event(
        session_id=parent.session_id,
        kind="turn",
        payload={"kind": "turn", "role": "user", "content": "hi"},
    )
    app = create_app(cfg, store=store, blob_store=blob_store, auto_namer=stub_namer)
    with TestClient(app) as client:
        resp = client.post(
            f"/api/sessions/{parent.session_id}/branch",
            json={"agent_backend": "mock"},
        )
    assert resp.status_code == 201
    assert resp.json()["child"]["agent_backend"] == "mock"
