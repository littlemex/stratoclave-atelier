"""Unit tests for ``POST /api/memory/query`` (Stage K)."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from stratoclave_atelier.blobs import InMemoryBlobStore
from stratoclave_atelier.config import AtelierConfig
from stratoclave_atelier.core.types import Event
from stratoclave_atelier.db import InMemoryStore
from stratoclave_atelier.memory import MemoryService, NoopMemoryService
from stratoclave_atelier.server import create_app


class _RecordingMemory(MemoryService):
    """Capture every call to :meth:`retrieve` so tests can inspect args."""

    enabled = True

    def __init__(self, retrieval: str | None = "[canonical] rule X") -> None:
        self.retrieval = retrieval
        self.calls: list[dict[str, Any]] = []

    async def ingest_session(
        self,
        *,
        session_id: UUID,
        events: Sequence[Event],
    ) -> None:
        del session_id, events

    async def retrieve(
        self,
        *,
        query: str,
        top_k: int = 5,
        scope_session_ids: Sequence[UUID] | None = None,
    ) -> str | None:
        self.calls.append(
            {
                "query": query,
                "top_k": top_k,
                "scope_session_ids": (
                    tuple(scope_session_ids) if scope_session_ids is not None else None
                ),
            }
        )
        return self.retrieval

    async def aclose(self) -> None:
        return None


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture
def blob_store() -> InMemoryBlobStore:
    return InMemoryBlobStore()


def _client_with_memory(
    cfg: AtelierConfig,
    store: InMemoryStore,
    blob_store: InMemoryBlobStore,
    memory: MemoryService,
) -> Iterator[TestClient]:
    app = create_app(cfg, store=store, blob_store=blob_store, memory_service=memory)
    with TestClient(app) as client:
        yield client


@pytest.fixture
def recording_client(
    stub_config: AtelierConfig,
    store: InMemoryStore,
    blob_store: InMemoryBlobStore,
) -> Iterator[tuple[TestClient, _RecordingMemory]]:
    memory = _RecordingMemory()
    yield from (
        (client, memory) for client in _client_with_memory(stub_config, store, blob_store, memory)
    )


def test_memory_query_with_scope_forwards_uuids(
    recording_client: tuple[TestClient, _RecordingMemory],
) -> None:
    """``session_ids`` is parsed as UUIDs and forwarded as scope."""

    client, memory = recording_client
    sid_a = uuid4()
    sid_b = uuid4()
    resp = client.post(
        "/api/memory/query",
        json={
            "query": "how to X?",
            "session_ids": [str(sid_a), str(sid_b)],
            "top_k": 3,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True
    assert body["memory_block"] == "[canonical] rule X"
    assert {UUID(s) for s in body["queried_session_ids"]} == {sid_a, sid_b}

    assert len(memory.calls) == 1
    call = memory.calls[0]
    assert call["query"] == "how to X?"
    assert call["top_k"] == 3
    assert set(call["scope_session_ids"]) == {sid_a, sid_b}


def test_memory_query_without_scope_passes_none(
    recording_client: tuple[TestClient, _RecordingMemory],
) -> None:
    client, memory = recording_client
    resp = client.post("/api/memory/query", json={"query": "anything"})
    assert resp.status_code == 200
    assert resp.json()["queried_session_ids"] is None
    assert memory.calls[-1]["scope_session_ids"] is None


def test_memory_query_with_noop_returns_disabled(
    stub_config: AtelierConfig,
    store: InMemoryStore,
    blob_store: InMemoryBlobStore,
) -> None:
    """The Noop service still satisfies the contract: ``enabled=False, block=None``."""

    for client in _client_with_memory(stub_config, store, blob_store, NoopMemoryService()):
        resp = client.post(
            "/api/memory/query",
            json={"query": "anything", "session_ids": [str(uuid4())]},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["enabled"] is False
        assert body["memory_block"] is None


def test_memory_query_rejects_empty_query(
    recording_client: tuple[TestClient, _RecordingMemory],
) -> None:
    client, memory = recording_client
    resp = client.post("/api/memory/query", json={"query": ""})
    assert resp.status_code == 422
    assert memory.calls == []


def test_memory_query_rejects_invalid_uuid(
    recording_client: tuple[TestClient, _RecordingMemory],
) -> None:
    client, memory = recording_client
    resp = client.post(
        "/api/memory/query",
        json={"query": "hi", "session_ids": ["not-a-uuid"]},
    )
    assert resp.status_code == 422
    assert memory.calls == []


def test_memory_adopt_persists_block_and_peek_returns_it(
    recording_client: tuple[TestClient, _RecordingMemory],
) -> None:
    """Adopt -> peek round-trip surfaces the queued block."""

    client, _ = recording_client
    create = client.post("/api/sessions", json={"title": "adopt"})
    assert create.status_code == 201
    sid = create.json()["session_id"]

    resp = client.post(
        "/api/memory/adopt",
        json={
            "session_id": sid,
            "memory_block": "[canonical] adopted-rule",
        },
    )
    assert resp.status_code == 202
    assert resp.json()["pending"] is True

    peek = client.get(f"/api/memory/adopt/{sid}")
    assert peek.status_code == 200
    body = peek.json()
    assert body["pending"] is True
    assert body["memory_block"] == "[canonical] adopted-rule"


def test_memory_adopt_overwrites_previous_block(
    recording_client: tuple[TestClient, _RecordingMemory],
) -> None:
    client, _ = recording_client
    create = client.post("/api/sessions", json={"title": "adopt"})
    sid = create.json()["session_id"]

    client.post("/api/memory/adopt", json={"session_id": sid, "memory_block": "first"})
    client.post("/api/memory/adopt", json={"session_id": sid, "memory_block": "second"})

    peek = client.get(f"/api/memory/adopt/{sid}")
    assert peek.json()["memory_block"] == "second"


def test_memory_adopt_clear_drops_pending_block(
    recording_client: tuple[TestClient, _RecordingMemory],
) -> None:
    client, _ = recording_client
    create = client.post("/api/sessions", json={"title": "adopt"})
    sid = create.json()["session_id"]

    client.post("/api/memory/adopt", json={"session_id": sid, "memory_block": "block"})
    delete = client.delete(f"/api/memory/adopt/{sid}")
    assert delete.status_code == 200
    assert delete.json()["pending"] is False

    peek = client.get(f"/api/memory/adopt/{sid}")
    assert peek.json()["pending"] is False
    assert peek.json()["memory_block"] is None


def test_memory_adopt_unknown_session_returns_404(
    recording_client: tuple[TestClient, _RecordingMemory],
) -> None:
    client, _ = recording_client
    resp = client.post(
        "/api/memory/adopt",
        json={"session_id": str(uuid4()), "memory_block": "x"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_memory_adopt_on_frozen_session_returns_409(
    recording_client: tuple[TestClient, _RecordingMemory],
    store: InMemoryStore,
) -> None:
    """Adopting against a frozen session is rejected (no future agent run will consume it)."""

    client, _ = recording_client
    create = client.post("/api/sessions", json={"title": "adopt"})
    sid = create.json()["session_id"]
    await store.update_session_status(UUID(sid), "frozen")

    resp = client.post(
        "/api/memory/adopt",
        json={"session_id": sid, "memory_block": "x"},
    )
    assert resp.status_code == 409


def test_memory_adopt_rejects_empty_block(
    recording_client: tuple[TestClient, _RecordingMemory],
) -> None:
    client, _ = recording_client
    create = client.post("/api/sessions", json={"title": "adopt"})
    sid = create.json()["session_id"]
    resp = client.post(
        "/api/memory/adopt",
        json={"session_id": sid, "memory_block": ""},
    )
    assert resp.status_code == 422


def test_memory_peek_with_no_pending_block_returns_false(
    recording_client: tuple[TestClient, _RecordingMemory],
) -> None:
    client, _ = recording_client
    resp = client.get(f"/api/memory/adopt/{uuid4()}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["pending"] is False
    assert body["memory_block"] is None
