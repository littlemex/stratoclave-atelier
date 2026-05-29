"""Unit tests for ``POST /api/curator/query`` (Stage L)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from stratoclave_loom import AcpChunk

from stratoclave_atelier.blobs import InMemoryBlobStore
from stratoclave_atelier.config import AtelierConfig
from stratoclave_atelier.curator import (
    CuratorContextError,
    CuratorRunner,
    CuratorScopeError,
)
from stratoclave_atelier.db import InMemoryStore
from stratoclave_atelier.server import create_app


class _StubCuratorRunner:
    """Replace ``CuratorRunner`` with a programmable async-iter source.

    The real runner spawns a loom session against an actual backend cwd,
    which we cannot do from a unit test. The router only depends on
    ``curate(...)`` returning an async iterator of :class:`AcpChunk`, so
    we hand-roll one and feed it the chunks the test wants.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.chunks: list[AcpChunk] = []
        self.raises: Exception | None = None

    async def curate(
        self,
        *,
        scope_kind: str,
        scope_id: Any,
        context_mode: str,
        question: str,
        backend: str | None = None,
    ) -> AsyncIterator[AcpChunk]:
        self.calls.append(
            {
                "scope_kind": scope_kind,
                "scope_id": str(scope_id),
                "context_mode": context_mode,
                "question": question,
                "backend": backend,
            }
        )
        if self.raises is not None:
            raise self.raises

        chunks = list(self.chunks)

        async def _gen() -> AsyncIterator[AcpChunk]:
            for chunk in chunks:
                yield chunk

        return _gen()


@pytest.fixture
def stub_runner() -> _StubCuratorRunner:
    return _StubCuratorRunner()


@pytest.fixture
def client(
    stub_config: AtelierConfig,
    stub_runner: _StubCuratorRunner,
) -> Iterator[TestClient]:
    """Build the app, then swap in our stub runner before the test runs."""

    app = create_app(stub_config, store=InMemoryStore(), blob_store=InMemoryBlobStore())
    with TestClient(app) as c:
        # The lifespan installed a real CuratorRunner; replace it after
        # the lifespan is up so deps that read app.state see our stub.
        app.state.curator_runner = stub_runner
        yield c


def _make_chunk(chunk_type: str, content: dict[str, Any]) -> AcpChunk:
    return AcpChunk(
        session_id="curator-stub",
        chunk_type=chunk_type,
        content=content,
    )


def _parse_sse(body: str) -> list[tuple[str, str]]:
    """Return ``(event, data)`` pairs from a minimal SSE response body."""

    frames: list[tuple[str, str]] = []
    for raw in body.split("\n\n"):
        block = raw.strip()
        if not block:
            continue
        event = ""
        data = ""
        for line in block.split("\n"):
            if line.startswith("event:"):
                event = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data = line[len("data:") :].strip()
        frames.append((event, data))
    return frames


def test_curator_query_streams_text_deltas(
    client: TestClient,
    stub_runner: _StubCuratorRunner,
) -> None:
    """Happy path: stream chunks come back as SSE frames."""

    stub_runner.chunks = [
        _make_chunk("text_delta", {"text": "Hel"}),
        _make_chunk("text_delta", {"text": "lo"}),
    ]

    resp = client.post(
        "/api/curator/query",
        json={
            "scope_kind": "session",
            "scope_id": str(uuid4()),
            "context_mode": "raw",
            "question": "summarise please",
        },
    )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    frames = _parse_sse(resp.text)
    events = [e for e, _ in frames]
    assert events == ["text_delta", "text_delta", "end_turn"]
    # text_delta payloads carry the text we fed in.
    assert '"text":"Hel"' in frames[0][1]
    assert '"text":"lo"' in frames[1][1]


def test_curator_query_returns_404_on_scope_error(
    client: TestClient,
    stub_runner: _StubCuratorRunner,
) -> None:
    stub_runner.raises = CuratorScopeError("group X not found")

    resp = client.post(
        "/api/curator/query",
        json={
            "scope_kind": "group",
            "scope_id": str(uuid4()),
            "context_mode": "raw",
            "question": "anything",
        },
    )

    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"]


def test_curator_query_returns_503_when_disabled(
    client: TestClient,
    stub_runner: _StubCuratorRunner,
) -> None:
    stub_runner.raises = CuratorContextError("agent backend disabled")

    resp = client.post(
        "/api/curator/query",
        json={
            "scope_kind": "session",
            "scope_id": str(uuid4()),
            "context_mode": "raw",
            "question": "hi",
        },
    )

    assert resp.status_code == 503
    assert "disabled" in resp.json()["detail"]


def test_curator_query_validates_question_required(client: TestClient) -> None:
    resp = client.post(
        "/api/curator/query",
        json={
            "scope_kind": "session",
            "scope_id": str(uuid4()),
            "context_mode": "raw",
            "question": "",
        },
    )
    assert resp.status_code == 422


def test_curator_query_rejects_bad_scope_kind(client: TestClient) -> None:
    resp = client.post(
        "/api/curator/query",
        json={
            "scope_kind": "bogus",
            "scope_id": str(uuid4()),
            "context_mode": "raw",
            "question": "hi",
        },
    )
    assert resp.status_code == 422


def test_curator_query_forwards_payload_to_runner(
    client: TestClient,
    stub_runner: _StubCuratorRunner,
) -> None:
    sid = uuid4()
    resp = client.post(
        "/api/curator/query",
        json={
            "scope_kind": "session",
            "scope_id": str(sid),
            "context_mode": "distill",
            "question": "what changed?",
            "backend": "mock",
        },
    )
    assert resp.status_code == 200
    assert stub_runner.calls == [
        {
            "scope_kind": "session",
            "scope_id": str(sid),
            "context_mode": "distill",
            "question": "what changed?",
            "backend": "mock",
        }
    ]


def test_curator_query_default_context_mode_is_raw(
    client: TestClient,
    stub_runner: _StubCuratorRunner,
) -> None:
    resp = client.post(
        "/api/curator/query",
        json={
            "scope_kind": "session",
            "scope_id": str(uuid4()),
            "question": "hi",
        },
    )
    assert resp.status_code == 200
    assert stub_runner.calls[-1]["context_mode"] == "raw"


def test_curator_runner_disabled_when_no_backend(stub_config: AtelierConfig) -> None:
    """The real runner reports disabled when no backend is configured."""

    runner = CuratorRunner(
        config=stub_config,
        store=InMemoryStore(),
        memory=_NullMemory(),
    )
    assert runner.enabled is False


class _NullMemory:
    enabled = False

    async def ingest_session(self, **_: Any) -> None:
        return None

    async def retrieve(self, **_: Any) -> str | None:
        return None

    async def aclose(self) -> None:
        return None
