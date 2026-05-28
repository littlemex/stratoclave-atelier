"""Tests for ``/api/groups`` REST endpoints."""

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from stratoclave_atelier.config import AtelierConfig
from stratoclave_atelier.db import InMemoryStore
from stratoclave_atelier.server import create_app


@pytest.fixture
def client(stub_config: AtelierConfig) -> Iterator[TestClient]:
    store = InMemoryStore()
    app = create_app(stub_config, store=store)
    with TestClient(app) as client:
        yield client


def test_create_group_returns_201_and_payload(client: TestClient) -> None:
    resp = client.post(
        "/api/groups",
        json={"name": "ops", "description": "team ops", "color": "#3B82F6"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "ops"
    assert body["description"] == "team ops"
    assert body["color"] == "#3B82F6"


def test_list_groups_returns_created_groups(client: TestClient) -> None:
    client.post("/api/groups", json={"name": "a", "description": None, "color": "#10B981"})
    client.post("/api/groups", json={"name": "b", "description": None, "color": "#F59E0B"})
    resp = client.get("/api/groups")
    assert resp.status_code == 200
    body = resp.json()
    names = [g["name"] for g in body]
    assert names == ["a", "b"]
    colors = [g["color"] for g in body]
    assert colors == ["#10B981", "#F59E0B"]


def test_get_group_unknown_returns_404(client: TestClient) -> None:
    resp = client.get(f"/api/groups/{uuid4()}")
    assert resp.status_code == 404


def test_create_group_rejects_empty_name(client: TestClient) -> None:
    resp = client.post(
        "/api/groups",
        json={"name": "", "description": None, "color": "#3B82F6"},
    )
    assert resp.status_code == 422


def test_create_group_rejects_invalid_color(client: TestClient) -> None:
    resp = client.post(
        "/api/groups",
        json={"name": "a", "description": None, "color": "blue"},
    )
    assert resp.status_code == 422


def test_patch_group_renames_and_recolours(client: TestClient) -> None:
    created = client.post(
        "/api/groups",
        json={"name": "a", "description": None, "color": "#3B82F6"},
    ).json()
    gid = created["group_id"]

    resp = client.patch(f"/api/groups/{gid}", json={"name": "b", "color": "#EF4444"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "b"
    assert body["color"] == "#EF4444"


def test_patch_group_rejects_empty_body(client: TestClient) -> None:
    created = client.post(
        "/api/groups",
        json={"name": "a", "description": None, "color": "#3B82F6"},
    ).json()
    gid = created["group_id"]

    resp = client.patch(f"/api/groups/{gid}", json={})
    assert resp.status_code == 409


def test_delete_group_returns_204_and_detaches_sessions(client: TestClient) -> None:
    group = client.post(
        "/api/groups",
        json={"name": "a", "description": None, "color": "#3B82F6"},
    ).json()
    gid = group["group_id"]
    session = client.post(
        "/api/sessions",
        json={"title": "root", "group_id": gid},
    ).json()
    sid = session["session_id"]

    resp = client.delete(f"/api/groups/{gid}")
    assert resp.status_code == 204

    detached = client.get(f"/api/sessions/{sid}").json()
    assert detached["group_id"] is None


def test_delete_unknown_group_returns_404(client: TestClient) -> None:
    resp = client.delete(f"/api/groups/{uuid4()}")
    assert resp.status_code == 404


def test_assign_session_to_group(client: TestClient) -> None:
    group = client.post(
        "/api/groups",
        json={"name": "a", "description": None, "color": "#3B82F6"},
    ).json()
    session = client.post("/api/sessions", json={"title": "root"}).json()

    resp = client.put(
        f"/api/sessions/{session['session_id']}/group",
        json={"group_id": group["group_id"]},
    )
    assert resp.status_code == 200
    assert resp.json()["group_id"] == group["group_id"]


def test_assign_session_to_none_detaches(client: TestClient) -> None:
    group = client.post(
        "/api/groups",
        json={"name": "a", "description": None, "color": "#3B82F6"},
    ).json()
    session = client.post(
        "/api/sessions",
        json={"title": "root", "group_id": group["group_id"]},
    ).json()

    resp = client.put(
        f"/api/sessions/{session['session_id']}/group",
        json={"group_id": None},
    )
    assert resp.status_code == 200
    assert resp.json()["group_id"] is None


def test_assign_session_to_unknown_group_returns_404(client: TestClient) -> None:
    session = client.post("/api/sessions", json={"title": "root"}).json()
    resp = client.put(
        f"/api/sessions/{session['session_id']}/group",
        json={"group_id": str(uuid4())},
    )
    assert resp.status_code == 404


def test_assign_fork_session_to_group_returns_409(client: TestClient) -> None:
    """Forks (non-root sessions) must not be re-grouped independently."""

    group = client.post(
        "/api/groups",
        json={"name": "a", "description": None, "color": "#3B82F6"},
    ).json()
    parent = client.post("/api/sessions", json={"title": "root"}).json()

    # A fresh root session has no turns yet; freeze requires at least
    # one. Append a turn so freeze can produce a non-empty version.
    client.post(
        f"/api/sessions/{parent['session_id']}/turns",
        json={"role": "user", "content": "hello"},
    )
    freeze_resp = client.post(
        f"/api/sessions/{parent['session_id']}/freeze",
        json={"label": "v1"},
    )
    assert freeze_resp.status_code == 201
    version_id = freeze_resp.json()["version_id"]

    fork_resp = client.post(
        f"/api/sessions/{parent['session_id']}/fork",
        json={
            "title": "child",
            "parent_version_id": version_id,
            "fork_seq": 0,
        },
    )
    assert fork_resp.status_code == 201
    child_id = fork_resp.json()["session_id"]

    resp = client.put(
        f"/api/sessions/{child_id}/group",
        json={"group_id": group["group_id"]},
    )
    assert resp.status_code == 409
    assert "root" in resp.json()["detail"].lower()
