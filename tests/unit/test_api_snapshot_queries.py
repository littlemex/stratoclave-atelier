"""Tests for the ``/api/sessions/{id}/snapshot-query`` REST endpoints."""

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from stratoclave_atelier.blobs import BlobStore
from stratoclave_atelier.config import AtelierConfig
from stratoclave_atelier.core import Version
from stratoclave_atelier.db import InMemoryStore, Store
from stratoclave_atelier.server import create_app
from stratoclave_atelier.snapshot_resolver import SnapshotResolver


class RecordingResolver(SnapshotResolver):
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def resolve(
        self,
        *,
        store: Store,
        blob_store: BlobStore,
        version: Version,
        query: str,
    ) -> str:
        self.calls.append({"version_id": version.version_id, "query": query})
        return f"answer:{query}"


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture
def resolver() -> RecordingResolver:
    return RecordingResolver()


@pytest.fixture
def client(
    stub_config: AtelierConfig, store: InMemoryStore, resolver: RecordingResolver
) -> Iterator[TestClient]:
    app = create_app(stub_config, store=store, snapshot_resolver=resolver)
    with TestClient(app) as client:
        yield client


@pytest.mark.asyncio
async def test_snapshot_query_persists_and_resolves(
    client: TestClient,
    store: InMemoryStore,
    resolver: RecordingResolver,
) -> None:
    target_session = await store.create_session(title="target")
    version = await store.create_version(
        session_id=target_session.session_id,
        blob_sha="a" * 64,
        blob_path="t.jsonl",
        start_seq=0,
        end_seq=3,
        byte_size=10,
        label="v1",
    )
    source_session = await store.create_session(title="source")

    resp = client.post(
        f"/api/sessions/{source_session.session_id}/snapshot-query",
        json={"target_version_id": str(version.version_id), "query": "what was decided?"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["source_session_id"] == str(source_session.session_id)
    assert body["target_version_id"] == str(version.version_id)
    assert body["query"] == "what was decided?"
    assert body["response"] == "answer:what was decided?"

    assert len(resolver.calls) == 1
    assert resolver.calls[0]["query"] == "what was decided?"

    listed = client.get(
        "/api/snapshot-queries",
        params={"target_version_id": str(version.version_id)},
    ).json()
    assert len(listed) == 1
    assert listed[0]["query_id"] == body["query_id"]


def test_snapshot_query_unknown_session_returns_404(client: TestClient) -> None:
    resp = client.post(
        f"/api/sessions/{uuid4()}/snapshot-query",
        json={"target_version_id": str(uuid4()), "query": "?"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_snapshot_query_unknown_version_returns_404(
    client: TestClient, store: InMemoryStore
) -> None:
    source_session = await store.create_session(title="source")
    resp = client.post(
        f"/api/sessions/{source_session.session_id}/snapshot-query",
        json={"target_version_id": str(uuid4()), "query": "?"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_snapshot_queries_filters_by_source(
    client: TestClient, store: InMemoryStore
) -> None:
    target = await store.create_session(title="target")
    v = await store.create_version(
        session_id=target.session_id,
        blob_sha="b" * 64,
        blob_path="t.jsonl",
        start_seq=0,
        end_seq=1,
        byte_size=5,
    )
    source_a = await store.create_session(title="a")
    source_b = await store.create_session(title="b")
    client.post(
        f"/api/sessions/{source_a.session_id}/snapshot-query",
        json={"target_version_id": str(v.version_id), "query": "qa"},
    )
    client.post(
        f"/api/sessions/{source_b.session_id}/snapshot-query",
        json={"target_version_id": str(v.version_id), "query": "qb"},
    )

    listed = client.get(
        "/api/snapshot-queries",
        params={"source_session_id": str(source_a.session_id)},
    ).json()
    assert len(listed) == 1
    assert listed[0]["source_session_id"] == str(source_a.session_id)
