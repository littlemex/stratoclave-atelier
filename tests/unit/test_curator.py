"""Unit tests for the Stage L Curator backend (scope + context builders)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID, uuid4

import pytest

from stratoclave_atelier.core import NotFoundError
from stratoclave_atelier.core.types import Event
from stratoclave_atelier.curator import (
    CuratorContextError,
    CuratorScopeError,
    build_distill_context,
    build_raw_context,
    render_system_prompt,
    resolve_scope_sessions,
    resolve_session_chain,
)
from stratoclave_atelier.db import InMemoryStore
from stratoclave_atelier.memory import MemoryService


class _FakeMemory(MemoryService):
    """Records retrieve() calls and returns a canned block."""

    def __init__(self, retrieval: str | None = "[memo] X", enabled: bool = True) -> None:
        self.retrieval = retrieval
        self._enabled = enabled
        self.calls: list[dict[str, Any]] = []

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def ingest_session(
        self,
        *,
        session_id: UUID,
        events: Sequence[Event],
    ) -> None:
        del session_id, events

    async def retrieve(
        self,
        *,
        query: str,
        top_k: int = 5,
        scope_session_ids: Sequence[UUID] | None = None,
    ) -> str | None:
        self.calls.append(
            {
                "query": query,
                "top_k": top_k,
                "scope_session_ids": (
                    tuple(scope_session_ids) if scope_session_ids is not None else None
                ),
            }
        )
        return self.retrieval

    async def aclose(self) -> None:
        return None


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


# ---------------------------------------------------------------------------
# resolve_session_chain
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_session_chain_returns_root_first(store: InMemoryStore) -> None:
    root = await store.create_session(title="root")
    child = await store.create_session(title="child", parent_session_id=root.session_id)
    grand = await store.create_session(title="grand", parent_session_id=child.session_id)

    chain = await resolve_session_chain(store, grand.session_id)
    titles = [s.title for s in chain]
    assert titles == ["root", "child", "grand"]


@pytest.mark.asyncio
async def test_resolve_session_chain_unknown_returns_empty(store: InMemoryStore) -> None:
    chain = await resolve_session_chain(store, uuid4())
    assert chain == []


# ---------------------------------------------------------------------------
# resolve_scope_sessions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_scope_sessions_group(store: InMemoryStore) -> None:
    group = await store.create_group(name="g1", description=None, color="#ff0000")
    a = await store.create_session(title="a", group_id=group.group_id)
    b = await store.create_session(title="b", group_id=group.group_id)
    other = await store.create_session(title="other")

    sessions = await resolve_scope_sessions(store, scope_kind="group", scope_id=group.group_id)
    ids = {s.session_id for s in sessions}
    assert ids == {a.session_id, b.session_id}
    assert other.session_id not in ids


@pytest.mark.asyncio
async def test_resolve_scope_sessions_session_returns_chain(store: InMemoryStore) -> None:
    root = await store.create_session(title="root")
    child = await store.create_session(title="child", parent_session_id=root.session_id)

    sessions = await resolve_scope_sessions(store, scope_kind="session", scope_id=child.session_id)
    titles = [s.title for s in sessions]
    assert titles == ["root", "child"]


@pytest.mark.asyncio
async def test_resolve_scope_sessions_session_unknown_raises(store: InMemoryStore) -> None:
    with pytest.raises(CuratorScopeError):
        await resolve_scope_sessions(store, scope_kind="session", scope_id=uuid4())


@pytest.mark.asyncio
async def test_resolve_scope_sessions_group_unknown_raises(store: InMemoryStore) -> None:
    with pytest.raises(CuratorScopeError):
        await resolve_scope_sessions(store, scope_kind="group", scope_id=uuid4())


@pytest.mark.asyncio
async def test_resolve_scope_sessions_unsupported_kind(store: InMemoryStore) -> None:
    with pytest.raises(CuratorScopeError):
        await resolve_scope_sessions(
            store,
            scope_kind="bogus",
            scope_id=uuid4(),  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# build_distill_context
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_distill_context_forwards_scope(store: InMemoryStore) -> None:
    a = await store.create_session(title="a")
    b = await store.create_session(title="b")
    memory = _FakeMemory(retrieval="[memo] hit")

    block = await build_distill_context(memory, sessions=[a, b], question="how to X?")

    assert block == "[memo] hit"
    assert memory.calls[-1]["query"] == "how to X?"
    assert memory.calls[-1]["top_k"] == 10
    assert set(memory.calls[-1]["scope_session_ids"]) == {a.session_id, b.session_id}


@pytest.mark.asyncio
async def test_build_distill_context_disabled_raises() -> None:
    memory = _FakeMemory(retrieval=None, enabled=False)
    with pytest.raises(CuratorContextError):
        await build_distill_context(memory, sessions=[], question="q")


@pytest.mark.asyncio
async def test_build_distill_context_empty_match_returns_placeholder() -> None:
    memory = _FakeMemory(retrieval=None)
    block = await build_distill_context(memory, sessions=[], question="q")
    assert "no distilled memory matched" in block


# ---------------------------------------------------------------------------
# build_raw_context
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_raw_context_includes_turn_events(store: InMemoryStore) -> None:
    session = await store.create_session(title="raw-session")
    await store.append_event(
        session_id=session.session_id,
        kind="turn",
        payload={"role": "user", "content": "hello"},
    )
    await store.append_event(
        session_id=session.session_id,
        kind="agent_turn",
        payload={"role": "assistant", "content": "hi back"},
    )
    # agent_chunk noise should be filtered out
    await store.append_event(
        session_id=session.session_id,
        kind="agent_chunk",
        payload={"chunk_type": "text_delta", "text": "filler"},
    )

    block = await build_raw_context(store, sessions=[session])

    assert "hello" in block
    assert "hi back" in block
    assert "filler" not in block
    assert "## session raw-session" in block


@pytest.mark.asyncio
async def test_build_raw_context_caps_per_session(store: InMemoryStore) -> None:
    session = await store.create_session(title="capped")
    for i in range(5):
        await store.append_event(
            session_id=session.session_id,
            kind="turn",
            payload={"role": "user", "content": f"msg{i}"},
        )

    block = await build_raw_context(store, sessions=[session], max_events_per_session=2)

    assert "msg3" in block
    assert "msg4" in block
    assert "msg0" not in block
    assert "msg1" not in block


@pytest.mark.asyncio
async def test_build_raw_context_handles_empty_session(store: InMemoryStore) -> None:
    session = await store.create_session(title="empty")
    block = await build_raw_context(store, sessions=[session])
    assert "(no turn events)" in block


@pytest.mark.asyncio
async def test_build_raw_context_no_sessions_returns_placeholder(
    store: InMemoryStore,
) -> None:
    block = await build_raw_context(store, sessions=[])
    assert block == "(scope had no sessions)"


# ---------------------------------------------------------------------------
# render_system_prompt
# ---------------------------------------------------------------------------


def test_render_system_prompt_embeds_context() -> None:
    rendered = render_system_prompt("hello-context")
    assert "hello-context" in rendered
    assert "Curator" in rendered
    assert "BEGIN CONTEXT" in rendered
    assert "END CONTEXT" in rendered


# ---------------------------------------------------------------------------
# Smoke tests for typed errors
# ---------------------------------------------------------------------------


def test_scope_error_subclass_of_value_error() -> None:
    assert issubclass(CuratorScopeError, ValueError)


def test_context_error_subclass_of_runtime_error() -> None:
    assert issubclass(CuratorContextError, RuntimeError)


# Sanity: NotFoundError import path works for upstream code.
def test_not_found_error_resolves() -> None:
    assert NotFoundError.__name__ == "NotFoundError"
