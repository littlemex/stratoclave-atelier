"""Tests for the ``fork-graph`` REST endpoints."""

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


@pytest.mark.asyncio
async def test_group_fork_graph_collects_sessions_and_edges(
    client: TestClient, store: InMemoryStore
) -> None:
    group = await store.create_group(name="g", description=None)
    parent = await store.create_session(title="parent", group_id=group.group_id)
    parent_version = await store.create_version(
        session_id=parent.session_id,
        blob_sha="a" * 64,
        blob_path="parent.jsonl",
        start_seq=0,
        end_seq=4,
        byte_size=20,
        label="baseline",
    )
    child = await store.create_session(
        title="child",
        group_id=group.group_id,
        parent_session_id=parent.session_id,
        parent_version_id=parent_version.version_id,
        fork_seq=2,
    )

    resp = client.get(f"/api/groups/{group.group_id}/fork-graph")
    assert resp.status_code == 200
    body = resp.json()

    node_ids = {n["session_id"] for n in body["nodes"]}
    assert node_ids == {str(parent.session_id), str(child.session_id)}

    parent_node = next(n for n in body["nodes"] if n["session_id"] == str(parent.session_id))
    assert len(parent_node["versions"]) == 1
    assert parent_node["versions"][0]["label"] == "baseline"

    assert len(body["edges"]) == 1
    edge = body["edges"][0]
    assert edge["parent_session_id"] == str(parent.session_id)
    assert edge["child_session_id"] == str(child.session_id)
    assert edge["fork_seq"] == 2


def test_group_fork_graph_unknown_group_returns_404(client: TestClient) -> None:
    resp = client.get(f"/api/groups/{uuid4()}/fork-graph")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_session_fork_graph_includes_descendants(
    client: TestClient, store: InMemoryStore
) -> None:
    parent = await store.create_session(title="parent")
    parent_v = await store.create_version(
        session_id=parent.session_id,
        blob_sha="b" * 64,
        blob_path="p.jsonl",
        start_seq=0,
        end_seq=3,
        byte_size=10,
    )
    child = await store.create_session(
        title="child",
        parent_session_id=parent.session_id,
        parent_version_id=parent_v.version_id,
        fork_seq=1,
    )
    # An unrelated sibling root that must NOT appear.
    sibling = await store.create_session(title="unrelated")

    resp = client.get(f"/api/sessions/{parent.session_id}/fork-graph")
    assert resp.status_code == 200
    body = resp.json()
    node_ids = {n["session_id"] for n in body["nodes"]}
    assert node_ids == {str(parent.session_id), str(child.session_id)}
    assert str(sibling.session_id) not in node_ids


def test_session_fork_graph_unknown_session_returns_404(client: TestClient) -> None:
    resp = client.get(f"/api/sessions/{uuid4()}/fork-graph")
    assert resp.status_code == 404
