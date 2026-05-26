"""Tests for ``GET /api/agent/backends``.

Stage H lets the SPA discover which backends the operator has greenlit
for this deployment, so the picker can render them and pre-select the
default. The endpoint is read-only; it should never 500 even when the
server has no backend configured (it returns ``{"backends": [], "default": null}``).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from stratoclave_atelier.config import AtelierConfig
from stratoclave_atelier.db import InMemoryStore
from stratoclave_atelier.server import create_app


def _client(cfg: AtelierConfig) -> Iterator[TestClient]:
    app = create_app(cfg, store=InMemoryStore())
    with TestClient(app) as client:
        yield client


@pytest.fixture
def disabled_client() -> Iterator[TestClient]:
    cfg = AtelierConfig.from_env(
        {"ATELIER_DATABASE_URL": "postgresql+asyncpg://a:b@localhost:5432/c"}
    )
    yield from _client(cfg)


@pytest.fixture
def single_backend_client() -> Iterator[TestClient]:
    """Stage G back-compat: only ATELIER_AGENT_BACKEND configured."""

    cfg = AtelierConfig.from_env(
        {
            "ATELIER_DATABASE_URL": "postgresql+asyncpg://a:b@localhost:5432/c",
            "ATELIER_AGENT_BACKEND": "claude_code",
            "ATELIER_AGENT_CWD": "/tmp/wk",
        }
    )
    yield from _client(cfg)


@pytest.fixture
def multi_backend_client() -> Iterator[TestClient]:
    cfg = AtelierConfig.from_env(
        {
            "ATELIER_DATABASE_URL": "postgresql+asyncpg://a:b@localhost:5432/c",
            "ATELIER_AGENT_BACKENDS_ALLOWED": "claude_code,kiro_code",
            "ATELIER_AGENT_CWD_CLAUDE_CODE": "/tmp/cc",
            "ATELIER_AGENT_CWD_KIRO_CODE": "/tmp/kc",
            "ATELIER_AGENT_BACKEND": "kiro_code",
        }
    )
    yield from _client(cfg)


def test_list_backends_empty_when_disabled(disabled_client: TestClient) -> None:
    resp = disabled_client.get("/api/agent/backends")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"backends": [], "default": None}


def test_list_backends_single_default_visible(single_backend_client: TestClient) -> None:
    resp = single_backend_client.get("/api/agent/backends")
    assert resp.status_code == 200
    body = resp.json()
    assert body["default"] == "claude_code"
    assert len(body["backends"]) == 1
    entry = body["backends"][0]
    assert entry["name"] == "claude_code"
    assert entry["ready"] is True
    assert entry["cwd"] == "/tmp/wk"


def test_list_backends_multi_with_default(multi_backend_client: TestClient) -> None:
    resp = multi_backend_client.get("/api/agent/backends")
    assert resp.status_code == 200
    body = resp.json()
    assert body["default"] == "kiro_code"
    names = [b["name"] for b in body["backends"]]
    assert names == ["claude_code", "kiro_code"]
    assert all(b["ready"] for b in body["backends"])
    assert {b["cwd"] for b in body["backends"]} == {"/tmp/cc", "/tmp/kc"}
