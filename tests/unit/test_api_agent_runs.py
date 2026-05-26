"""Unit tests for ``POST /api/sessions/{id}/agent-runs``."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator, Mapping
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from stratoclave_loom import (
    AcpChunk,
    AgentBackend,
    BackendConfig,
    PermissionRequest,
    register_backend,
)
from stratoclave_loom.core.types import NormalizedTurn

from stratoclave_atelier.blobs import InMemoryBlobStore
from stratoclave_atelier.config import AtelierConfig
from stratoclave_atelier.db import InMemoryStore
from stratoclave_atelier.server import create_app


class _StubBackend(AgentBackend):
    backend_name = "stub_test"

    async def initialize(
        self,
        session_id: str,
        config: BackendConfig,
        capabilities: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return capabilities

    async def send_message(
        self,
        session_id: str,
        content: str,
        *,
        context_files: tuple[str, ...] = (),
    ) -> AsyncIterator[AcpChunk]:
        async def _gen() -> AsyncIterator[AcpChunk]:
            yield AcpChunk(session_id=session_id, chunk_type="text_delta", content={"text": "ok"})
            yield AcpChunk(session_id=session_id, chunk_type="end_turn", content={})

        return _gen()

    async def cancel(self, session_id: str) -> None:
        return None

    async def close(self, session_id: str) -> None:
        return None

    async def handle_permission(self, request: PermissionRequest, granted: bool) -> None:
        return None

    def normalize(self, raw_line: str, seq: int) -> list[NormalizedTurn]:
        return []

    def resume_args(self, frozen_jsonl_path: str) -> tuple[str, ...]:
        return ()


@pytest.fixture(autouse=True)
def _register_stub() -> None:
    register_backend("stub_test", _StubBackend)


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture
def blob_store() -> InMemoryBlobStore:
    return InMemoryBlobStore()


@pytest.fixture
def runner_config(stub_env: Mapping[str, str], tmp_path: Any) -> AtelierConfig:
    cfg = AtelierConfig.from_env(
        {
            **stub_env,
            "ATELIER_AGENT_BACKEND": "claude_code",
            "ATELIER_AGENT_CWD": str(tmp_path),
        }
    )
    object.__setattr__(cfg, "agent_backend", "stub_test")
    return cfg


@pytest.fixture
def disabled_config(stub_env: Mapping[str, str]) -> AtelierConfig:
    return AtelierConfig.from_env(stub_env)


@pytest.fixture
def runner_client(
    runner_config: AtelierConfig,
    store: InMemoryStore,
    blob_store: InMemoryBlobStore,
) -> Iterator[TestClient]:
    app = create_app(runner_config, store=store, blob_store=blob_store)
    with TestClient(app) as client:
        yield client


@pytest.fixture
def disabled_client(
    disabled_config: AtelierConfig,
    store: InMemoryStore,
    blob_store: InMemoryBlobStore,
) -> Iterator[TestClient]:
    app = create_app(disabled_config, store=store, blob_store=blob_store)
    with TestClient(app) as client:
        yield client


@pytest.mark.asyncio
async def test_post_agent_run_returns_202_and_drains_stream(
    runner_client: TestClient, store: InMemoryStore
) -> None:
    session = await store.create_session(title="s")
    resp = runner_client.post(
        f"/api/sessions/{session.session_id}/agent-runs",
        json={"prompt": "hello"},
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "scheduled"

    # Wait for the fire-and-forget task to drain.
    for _ in range(50):
        events = await store.list_events(session.session_id)
        if any(e.kind == "agent_turn" for e in events):
            break
        await asyncio.sleep(0.02)
    kinds = [e.kind for e in await store.list_events(session.session_id)]
    assert "turn" in kinds
    assert "agent_chunk" in kinds
    assert "agent_turn" in kinds


def test_post_agent_run_when_backend_disabled_returns_503(
    disabled_client: TestClient, store: InMemoryStore
) -> None:
    # Need a session to pass the existence check; create via API instead.
    create = disabled_client.post("/api/sessions", json={"title": "s"})
    assert create.status_code == 201
    sid = create.json()["session_id"]
    resp = disabled_client.post(
        f"/api/sessions/{sid}/agent-runs",
        json={"prompt": "hi"},
    )
    assert resp.status_code == 503
    assert "agent backend disabled" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_post_agent_run_unknown_session_returns_404(runner_client: TestClient) -> None:
    resp = runner_client.post(
        f"/api/sessions/{uuid4()}/agent-runs",
        json={"prompt": "hi"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_post_agent_run_on_frozen_session_returns_409(
    runner_client: TestClient, store: InMemoryStore
) -> None:
    session = await store.create_session(title="s")
    await store.update_session_status(session.session_id, "frozen")
    resp = runner_client.post(
        f"/api/sessions/{session.session_id}/agent-runs",
        json={"prompt": "hi"},
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_cancel_agent_run_returns_204(
    runner_client: TestClient, store: InMemoryStore
) -> None:
    session = await store.create_session(title="s")
    resp = runner_client.post(f"/api/sessions/{session.session_id}/agent-runs/cancel")
    assert resp.status_code == 204


def test_post_agent_run_rejects_empty_prompt(
    runner_client: TestClient, store: InMemoryStore
) -> None:
    create = runner_client.post("/api/sessions", json={"title": "s"})
    sid = create.json()["session_id"]
    resp = runner_client.post(
        f"/api/sessions/{sid}/agent-runs",
        json={"prompt": ""},
    )
    assert resp.status_code == 422
