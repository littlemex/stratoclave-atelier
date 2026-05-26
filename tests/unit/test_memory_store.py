"""Unit tests for the in-memory Store implementation.

These tests double as the executable spec for the :class:`Store`
Protocol; the asyncpg-backed implementation is expected to behave the
same way.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from stratoclave_atelier import ConflictError, NotFoundError
from stratoclave_atelier.db import InMemoryStore


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


# groups -----------------------------------------------------------------------


async def test_create_and_list_groups(store: InMemoryStore) -> None:
    await store.create_group(name="A", description=None)
    b = await store.create_group(name="B", description="second")
    listed = await store.list_groups()
    assert [g.name for g in listed] == ["A", "B"]
    assert (await store.get_group(b.group_id)).description == "second"


async def test_get_unknown_group_raises(store: InMemoryStore) -> None:
    with pytest.raises(NotFoundError, match=r"group .* not found"):
        await store.get_group(uuid4())


# sessions ---------------------------------------------------------------------


async def test_create_session_without_parent(store: InMemoryStore) -> None:
    session = await store.create_session(title="root")
    assert session.parent_session_id is None
    assert session.parent_version_id is None
    assert session.fork_seq is None
    assert session.status == "active"


async def test_create_session_in_group(store: InMemoryStore) -> None:
    group = await store.create_group(name="g", description=None)
    session = await store.create_session(title="t", group_id=group.group_id)
    listed = await store.list_sessions(group_id=group.group_id)
    assert [s.session_id for s in listed] == [session.session_id]


async def test_create_session_with_unknown_group_raises(store: InMemoryStore) -> None:
    with pytest.raises(NotFoundError, match=r"group .* not found"):
        await store.create_session(title="t", group_id=uuid4())


async def test_update_session_status(store: InMemoryStore) -> None:
    session = await store.create_session(title="t")
    updated = await store.update_session_status(session.session_id, "frozen")
    assert updated.status == "frozen"
    assert updated.updated_at >= session.created_at


# versions ---------------------------------------------------------------------


async def test_create_version_computes_turn_count(store: InMemoryStore) -> None:
    session = await store.create_session(title="t")
    version = await store.create_version(
        session_id=session.session_id,
        blob_sha="a" * 64,
        blob_path="ab/cd/ef.jsonl",
        start_seq=2,
        end_seq=5,
        byte_size=100,
    )
    assert version.turn_count == 4


async def test_create_version_rejects_inverted_range(store: InMemoryStore) -> None:
    session = await store.create_session(title="t")
    with pytest.raises(ConflictError, match="end_seq must be"):
        await store.create_version(
            session_id=session.session_id,
            blob_sha="a" * 64,
            blob_path="x.jsonl",
            start_seq=5,
            end_seq=2,
            byte_size=10,
        )


async def test_list_versions_orders_newest_first(store: InMemoryStore) -> None:
    session = await store.create_session(title="t")
    first = await store.create_version(
        session_id=session.session_id,
        blob_sha="a" * 64,
        blob_path="a.jsonl",
        start_seq=0,
        end_seq=2,
        byte_size=10,
    )
    second = await store.create_version(
        session_id=session.session_id,
        blob_sha="b" * 64,
        blob_path="b.jsonl",
        start_seq=3,
        end_seq=5,
        byte_size=10,
    )
    listed = await store.list_versions(session.session_id)
    assert [v.version_id for v in listed] == [second.version_id, first.version_id]


# fork -------------------------------------------------------------------------


async def test_fork_session_from_version(store: InMemoryStore) -> None:
    parent = await store.create_session(title="parent")
    version = await store.create_version(
        session_id=parent.session_id,
        blob_sha="a" * 64,
        blob_path="x.jsonl",
        start_seq=0,
        end_seq=5,
        byte_size=10,
    )
    child = await store.create_session(
        title="child",
        parent_session_id=parent.session_id,
        parent_version_id=version.version_id,
        fork_seq=3,
    )
    assert child.parent_session_id == parent.session_id
    assert child.parent_version_id == version.version_id
    assert child.fork_seq == 3


async def test_fork_with_mismatched_parent_session_raises(
    store: InMemoryStore,
) -> None:
    a = await store.create_session(title="a")
    b = await store.create_session(title="b")
    version = await store.create_version(
        session_id=a.session_id,
        blob_sha="a" * 64,
        blob_path="x.jsonl",
        start_seq=0,
        end_seq=5,
        byte_size=10,
    )
    with pytest.raises(ConflictError, match="does not belong to parent_session_id"):
        await store.create_session(
            title="bad",
            parent_session_id=b.session_id,
            parent_version_id=version.version_id,
            fork_seq=2,
        )


async def test_fork_seq_outside_version_range_raises(
    store: InMemoryStore,
) -> None:
    parent = await store.create_session(title="parent")
    version = await store.create_version(
        session_id=parent.session_id,
        blob_sha="a" * 64,
        blob_path="x.jsonl",
        start_seq=2,
        end_seq=5,
        byte_size=10,
    )
    with pytest.raises(ConflictError, match="must lie within"):
        await store.create_session(
            title="child",
            parent_session_id=parent.session_id,
            parent_version_id=version.version_id,
            fork_seq=10,
        )


# events -----------------------------------------------------------------------


async def test_append_and_replay_events(store: InMemoryStore) -> None:
    session = await store.create_session(title="t")
    e0 = await store.append_event(
        session_id=session.session_id, kind="turn", payload={"role": "user"}
    )
    e1 = await store.append_event(
        session_id=session.session_id,
        kind="turn",
        payload={"role": "assistant"},
    )
    assert e0.seq == 0
    assert e1.seq == 1
    assert await store.next_seq(session.session_id) == 2

    replay = await store.list_events(session.session_id, from_seq=1)
    assert [e.seq for e in replay] == [1]


async def test_append_event_unknown_session_raises(store: InMemoryStore) -> None:
    with pytest.raises(NotFoundError, match=r"session .* not found"):
        await store.append_event(session_id=uuid4(), kind="turn", payload={})
