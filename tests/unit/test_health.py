"""Tests for the ``/healthz`` endpoint."""

from __future__ import annotations

from fastapi.testclient import TestClient

from stratoclave_atelier.config import AtelierConfig
from stratoclave_atelier.server import create_app


def test_healthz_returns_ok(stub_config: AtelierConfig) -> None:
    app = create_app(stub_config)
    client = TestClient(app)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_create_app_attaches_config(stub_config: AtelierConfig) -> None:
    app = create_app(stub_config)
    assert app.state.config is stub_config
