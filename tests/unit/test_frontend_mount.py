"""Test that the SPA is mounted at ``/`` and assets at ``/static``."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from stratoclave_atelier.config import AtelierConfig
from stratoclave_atelier.db import InMemoryStore
from stratoclave_atelier.server import create_app


@pytest.fixture
def client(stub_config: AtelierConfig) -> Iterator[TestClient]:
    app = create_app(stub_config, store=InMemoryStore())
    with TestClient(app) as client:
        yield client


def test_root_serves_index_html(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    assert "stratoclave-atelier" in resp.text
    assert "/static/js/app.js" in resp.text


def test_static_js_is_served(client: TestClient) -> None:
    resp = client.get("/static/js/app.js")
    assert resp.status_code == 200
    assert "openIngestSocket" in resp.text


def test_static_css_is_served(client: TestClient) -> None:
    resp = client.get("/static/css/app.css")
    assert resp.status_code == 200
    assert "graph-node" in resp.text
