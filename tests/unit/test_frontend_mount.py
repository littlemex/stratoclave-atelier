"""Test that Stage G chat is mounted at ``/`` and the panels SPA at ``/panels``."""

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


def test_root_serves_chat_index(client: TestClient) -> None:
    """Stage G chat ships at ``/``: minimal shell + chat.js + chat.css."""

    resp = client.get("/")
    assert resp.status_code == 200
    assert "stratoclave-atelier" in resp.text
    assert "/static/js/chat.js" in resp.text
    assert "/static/css/chat.css" in resp.text


def test_panels_serves_legacy_index(client: TestClient) -> None:
    """The Stage B-F four-panel UI is reachable at ``/panels``."""

    resp = client.get("/panels")
    assert resp.status_code == 200
    assert "/static/panels/js/app.js" in resp.text
    assert "/static/panels/css/app.css" in resp.text


def test_static_chat_js_is_served(client: TestClient) -> None:
    resp = client.get("/static/js/chat.js")
    assert resp.status_code == 200
    assert "agent-runs" in resp.text


def test_static_chat_css_is_served(client: TestClient) -> None:
    resp = client.get("/static/css/chat.css")
    assert resp.status_code == 200
    assert "chat-log" in resp.text


def test_static_panels_assets_are_served(client: TestClient) -> None:
    """Legacy panels assets continue to be served from ``/static/panels``."""

    js = client.get("/static/panels/js/app.js")
    assert js.status_code == 200
    assert "openIngestSocket" in js.text

    css = client.get("/static/panels/css/app.css")
    assert css.status_code == 200
    assert "graph-node" in css.text
