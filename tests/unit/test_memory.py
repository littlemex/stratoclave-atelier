"""Unit tests for the Stage G-4 :class:`MemoryService` abstraction.

Covers the parts that do not require stratoclave-distill:

* :class:`NoopMemoryService` returns ``None`` for every retrieve;
* :func:`build_memory_service` returns the noop when distill is
  disabled, regardless of whether the optional package is installed;
* the import-error fallback path demotes to noop when
  ``ATELIER_DISTILL_ENABLED`` is true but the import fails;
* :class:`AgentRunner` calls into the memory service when configured
  and threads the retrieved string into the user-turn payload.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any
from uuid import UUID, uuid4

import pytest
from stratoclave_loom import (
    AcpChunk,
    AgentBackend,
    BackendConfig,
    PermissionRequest,
    register_backend,
)
from stratoclave_loom.core.types import NormalizedTurn

from stratoclave_atelier.agent_runner import AgentRunner
from stratoclave_atelier.config import AtelierConfig
from stratoclave_atelier.core.types import Event
from stratoclave_atelier.db import InMemoryStore
from stratoclave_atelier.events_bus import EventBus
from stratoclave_atelier.memory import (
    MemoryService,
    NoopMemoryService,
    build_memory_service,
)


class _RecordingMemoryService(MemoryService):
    """Capture every memory call so tests can assert ordering / arguments."""

    enabled = True

    def __init__(self, retrieval: str | None = "fact A\nfact B") -> None:
        self.retrieval = retrieval
        self.queries: list[str] = []
        self.ingested: list[tuple[UUID, tuple[Event, ...]]] = []
        self.closed = False

    async def ingest_session(
        self,
        *,
        session_id: UUID,
        events: Sequence[Event],
    ) -> None:
        self.ingested.append((session_id, tuple(events)))

    async def retrieve(self, *, query: str, top_k: int = 5) -> str | None:
        self.queries.append(query)
        return self.retrieval

    async def aclose(self) -> None:
        self.closed = True


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


@pytest.fixture
def stub_backend() -> None:
    register_backend("stub_test", _StubBackend)


@pytest.mark.asyncio
async def test_noop_memory_service_returns_none() -> None:
    noop = NoopMemoryService()
    assert noop.enabled is False
    assert await noop.retrieve(query="anything") is None
    await noop.ingest_session(session_id=uuid4(), events=[])
    await noop.aclose()


@pytest.mark.asyncio
async def test_build_memory_service_returns_noop_when_disabled(
    stub_env: Mapping[str, str],
) -> None:
    cfg = AtelierConfig.from_env(stub_env)
    assert cfg.distill_enabled is False
    memory = await build_memory_service(cfg)
    assert isinstance(memory, NoopMemoryService)


@pytest.mark.asyncio
async def test_build_memory_service_falls_back_on_import_error(
    monkeypatch: pytest.MonkeyPatch,
    stub_env: Mapping[str, str],
) -> None:
    """When distill is enabled but the helper module is missing, demote to noop."""

    cfg = AtelierConfig.from_env(
        {
            **stub_env,
            "ATELIER_DISTILL_ENABLED": "true",
            "ATELIER_DISTILL_DATABASE_URL": "postgresql://stub/distill",
        }
    )
    assert cfg.distill_enabled is True

    sentinel = "stratoclave_atelier._distill_memory"

    # Setting ``sys.modules[name] = None`` is the canonical way to make a
    # subsequent ``from ... import`` raise ``ImportError`` -- the import
    # machinery treats ``None`` as "module is not importable".
    import sys

    monkeypatch.setitem(sys.modules, sentinel, None)
    memory = await build_memory_service(cfg)
    assert isinstance(memory, NoopMemoryService)


@pytest.mark.asyncio
async def test_runner_threads_memory_into_user_turn(
    stub_env: Mapping[str, str],
    stub_backend: None,
    tmp_path: Any,
) -> None:
    cfg = AtelierConfig.from_env(
        {
            **stub_env,
            "ATELIER_AGENT_BACKEND": "claude_code",
            "ATELIER_AGENT_CWD": str(tmp_path),
        }
    )
    object.__setattr__(cfg, "agent_backend", "stub_test")

    store = InMemoryStore()
    bus = EventBus()
    memory = _RecordingMemoryService(retrieval="canonical: rule X")
    runner = AgentRunner(config=cfg, store=store, bus=bus, memory=memory)
    session = await store.create_session(title="t")

    await runner.run(session_id=session.session_id, prompt="how to X?")

    assert memory.queries == ["how to X?"]
    events = await store.list_events(session.session_id)
    user_turn = next(e for e in events if e.kind == "turn")
    assert user_turn.payload["memory_used"] is True
    assert user_turn.payload["content"] == "how to X?"
    await runner.close()


@pytest.mark.asyncio
async def test_runner_skips_memory_when_disabled_via_config(
    stub_env: Mapping[str, str],
    stub_backend: None,
    tmp_path: Any,
) -> None:
    cfg = AtelierConfig.from_env(
        {
            **stub_env,
            "ATELIER_AGENT_BACKEND": "claude_code",
            "ATELIER_AGENT_CWD": str(tmp_path),
            "ATELIER_AGENT_MEMORY": "false",
        }
    )
    object.__setattr__(cfg, "agent_backend", "stub_test")

    store = InMemoryStore()
    bus = EventBus()
    memory = _RecordingMemoryService(retrieval="should-never-appear")
    runner = AgentRunner(config=cfg, store=store, bus=bus, memory=memory)
    session = await store.create_session(title="t")

    await runner.run(session_id=session.session_id, prompt="hi")
    assert memory.queries == []
    events = await store.list_events(session.session_id)
    user_turn = next(e for e in events if e.kind == "turn")
    assert user_turn.payload["memory_used"] is False
    await runner.close()


@pytest.mark.asyncio
async def test_runner_swallows_memory_errors(
    stub_env: Mapping[str, str],
    stub_backend: None,
    tmp_path: Any,
) -> None:
    class _ExplodingMemory(MemoryService):
        enabled = True

        async def ingest_session(self, *, session_id: UUID, events: Sequence[Event]) -> None:
            return None

        async def retrieve(self, *, query: str, top_k: int = 5) -> str | None:
            raise RuntimeError("memory unavailable")

        async def aclose(self) -> None:
            return None

    cfg = AtelierConfig.from_env(
        {
            **stub_env,
            "ATELIER_AGENT_BACKEND": "claude_code",
            "ATELIER_AGENT_CWD": str(tmp_path),
        }
    )
    object.__setattr__(cfg, "agent_backend", "stub_test")
    store = InMemoryStore()
    runner = AgentRunner(config=cfg, store=store, bus=EventBus(), memory=_ExplodingMemory())
    session = await store.create_session(title="t")
    await runner.run(session_id=session.session_id, prompt="hi")
    events = await store.list_events(session.session_id)
    user_turn = next(e for e in events if e.kind == "turn")
    assert user_turn.payload["memory_used"] is False
    await runner.close()


@pytest.mark.asyncio
async def test_freeze_invokes_memory_ingest(
    stub_env: Mapping[str, str],
) -> None:
    from stratoclave_atelier.blobs import InMemoryBlobStore
    from stratoclave_atelier.freeze import freeze_session

    cfg = AtelierConfig.from_env(stub_env)
    assert cfg is not None  # silence unused-var
    store = InMemoryStore()
    blob_store = InMemoryBlobStore()
    memory = _RecordingMemoryService()

    session = await store.create_session(title="t")
    await store.append_event(
        session_id=session.session_id,
        kind="turn",
        payload={"kind": "turn", "role": "user", "content": "hello"},
    )
    await store.append_event(
        session_id=session.session_id,
        kind="turn",
        payload={"kind": "turn", "role": "assistant", "content": "hi"},
    )

    await freeze_session(
        store=store,
        blob_store=blob_store,
        session_id=session.session_id,
        memory=memory,
    )
    assert len(memory.ingested) == 1
    sid, events = memory.ingested[0]
    assert sid == session.session_id
    assert all(e.kind == "turn" for e in events)
    assert len(events) == 2
