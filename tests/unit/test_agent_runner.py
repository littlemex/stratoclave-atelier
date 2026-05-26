"""Unit tests for :class:`AgentRunner` using a stub loom backend."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any

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
from stratoclave_atelier.db import InMemoryStore
from stratoclave_atelier.events_bus import EventBus


class _StubBackend(AgentBackend):
    """Loom backend stub that emits a configurable chunk script."""

    backend_name = "stub_test"

    def __init__(self) -> None:
        self.script: list[tuple[str, Mapping[str, Any]]] = [
            ("text_delta", {"text": "Hello"}),
            ("text_delta", {"text": ", world"}),
            ("end_turn", {}),
        ]
        self.cancelled: list[str] = []
        self.closed: list[str] = []
        self.raise_on_send: BaseException | None = None

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
        if self.raise_on_send is not None:
            raise self.raise_on_send

        async def _gen() -> AsyncIterator[AcpChunk]:
            for kind, payload in self.script:
                yield AcpChunk(session_id=session_id, chunk_type=kind, content=payload)

        return _gen()

    async def cancel(self, session_id: str) -> None:
        self.cancelled.append(session_id)

    async def close(self, session_id: str) -> None:
        self.closed.append(session_id)

    async def handle_permission(self, request: PermissionRequest, granted: bool) -> None:
        return None

    def normalize(self, raw_line: str, seq: int) -> list[NormalizedTurn]:
        return []

    def resume_args(self, frozen_jsonl_path: str) -> tuple[str, ...]:
        return ()


@pytest.fixture
def stub_backend() -> _StubBackend:
    backend = _StubBackend()
    register_backend("stub_test", lambda: backend)
    return backend


@pytest.fixture
def stub_runner_config(stub_env: Mapping[str, str], tmp_path: Any) -> AtelierConfig:
    return AtelierConfig.from_env(
        {
            **stub_env,
            "ATELIER_AGENT_BACKEND": "claude_code",  # any non-none satisfies the gate
            "ATELIER_AGENT_CWD": str(tmp_path),
        },
        agent_backend="claude_code",
    )


@pytest.mark.asyncio
async def test_runner_emits_chunks_then_summary(
    stub_runner_config: AtelierConfig,
    stub_backend: _StubBackend,
) -> None:
    # Override the backend literal: the runner reads agent_backend by name so
    # we substitute "stub_test" by mutating the config via from_env helper.
    cfg = AtelierConfig.from_env(
        {
            "ATELIER_DATABASE_URL": stub_runner_config.database_url,
            "ATELIER_AGENT_CWD": stub_runner_config.agent_cwd or ".",
        },
        agent_backend="claude_code",
    )
    # Sneak the stub backend name in via object.__setattr__ since the
    # dataclass is frozen and the Literal forbids "stub_test" at type level.
    object.__setattr__(cfg, "agent_backend", "stub_test")

    store = InMemoryStore()
    bus = EventBus()
    runner = AgentRunner(config=cfg, store=store, bus=bus)
    session = await store.create_session(title="test")

    await runner.run(session_id=session.session_id, prompt="hello")

    events = await store.list_events(session.session_id)
    kinds = [e.kind for e in events]
    assert kinds[0] == "turn"  # user turn first
    assert "agent_chunk" in kinds
    assert kinds[-1] == "agent_turn"

    chunks = [e for e in events if e.kind == "agent_chunk"]
    assert [c.payload["text"] for c in chunks] == ["Hello", ", world"]

    summary = events[-1]
    assert summary.payload["content"] == "Hello, world"
    assert summary.payload["role"] == "assistant"
    await runner.close()


@pytest.mark.asyncio
async def test_runner_records_error_event_on_send_failure(
    stub_runner_config: AtelierConfig,
    stub_backend: _StubBackend,
) -> None:
    cfg = stub_runner_config
    object.__setattr__(cfg, "agent_backend", "stub_test")

    stub_backend.raise_on_send = RuntimeError("boom")
    store = InMemoryStore()
    bus = EventBus()
    runner = AgentRunner(config=cfg, store=store, bus=bus)
    session = await store.create_session(title="test")

    await runner.run(session_id=session.session_id, prompt="hello")

    events = await store.list_events(session.session_id)
    assert events[0].kind == "turn"
    assert events[-1].kind == "agent_error"
    assert events[-1].payload["type"] == "RuntimeError"
    assert events[-1].payload["error"] == "boom"
    await runner.close()


@pytest.mark.asyncio
async def test_runner_disabled_when_backend_none(stub_env: Mapping[str, str]) -> None:
    cfg = AtelierConfig.from_env(stub_env)
    runner = AgentRunner(config=cfg, store=InMemoryStore(), bus=EventBus())
    assert runner.enabled is False
    with pytest.raises(RuntimeError, match="agent backend disabled"):
        from uuid import uuid4

        await runner.run(session_id=uuid4(), prompt="hi")


@pytest.mark.asyncio
async def test_runner_compose_prompt_injects_memory() -> None:
    composed = AgentRunner._compose_prompt("question", "fact A\nfact B")
    assert composed.startswith("<memory>\n")
    assert "fact A" in composed
    assert composed.endswith("question")


@pytest.mark.asyncio
async def test_runner_publishes_to_bus(
    stub_runner_config: AtelierConfig,
    stub_backend: _StubBackend,
) -> None:
    import asyncio

    cfg = stub_runner_config
    object.__setattr__(cfg, "agent_backend", "stub_test")
    store = InMemoryStore()
    bus = EventBus()
    runner = AgentRunner(config=cfg, store=store, bus=bus)
    session = await store.create_session(title="test")

    received: list[Any] = []
    async with bus.subscribe(session.session_id) as queue:

        async def collect() -> None:
            while True:
                item = await queue.get()
                if item is None:
                    return
                received.append(item)

        task = asyncio.create_task(collect())
        await runner.run(session_id=session.session_id, prompt="hi")
        # End-of-turn marker pulled by the runner; cancel the collector.
        await asyncio.sleep(0.05)
        task.cancel()
        import contextlib

        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    kinds = [e.kind for e in received]
    assert "turn" in kinds
    assert "agent_chunk" in kinds
    assert "agent_turn" in kinds
    await runner.close()
