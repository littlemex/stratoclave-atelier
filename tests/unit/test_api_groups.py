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
    resp = client.post("/api/groups", json={"name": "ops", "description": "team ops"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "ops"
    assert body["description"] == "team ops"


def test_list_groups_returns_created_groups(client: TestClient) -> None:
    client.post("/api/groups", json={"name": "a", "description": None})
    client.post("/api/groups", json={"name": "b", "description": None})
    resp = client.get("/api/groups")
    assert resp.status_code == 200
    names = [g["name"] for g in resp.json()]
    assert names == ["a", "b"]


def test_get_group_unknown_returns_404(client: TestClient) -> None:
    resp = client.get(f"/api/groups/{uuid4()}")
    assert resp.status_code == 404


def test_create_group_rejects_empty_name(client: TestClient) -> None:
    resp = client.post("/api/groups", json={"name": "", "description": None})
    assert resp.status_code == 422
