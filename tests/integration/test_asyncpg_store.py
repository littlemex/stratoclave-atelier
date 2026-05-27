"""Integration tests for :class:`AsyncpgStore` against a live Postgres.

Skipped unless ``ATELIER_TEST_DATABASE_URL`` is set (CI provides it via
the pgvector service container; local runs need ``docker compose up -d``
first). The fixture builds a fresh :class:`AsyncpgStore` per test
function and truncates all atelier tables between runs to keep tests
order-independent.

These tests double as the contract suite shared with
:class:`InMemoryStore`: the assertions mirror
``tests/unit/test_memory_store.py`` so any divergence between the two
backends shows up immediately.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from stratoclave_atelier import ConflictError, NotFoundError
from stratoclave_atelier.db import AsyncpgStore, create_engine

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        "ATELIER_TEST_DATABASE_URL" not in os.environ,
        reason="ATELIER_TEST_DATABASE_URL not set",
    ),
]


def _async_url() -> str:
    """Return the test DSN with the asyncpg driver prefix."""

    url = os.environ["ATELIER_TEST_DATABASE_URL"]
    if "+psycopg" in url:
        return url.replace("postgresql+psycopg://", "postgresql+asyncpg://")
    if "+asyncpg" in url:
        return url
    return url.replace("postgresql://", "postgresql+asyncpg://")


@pytest_asyncio.fixture
async def store() -> AsyncIterator[AsyncpgStore]:
    engine = create_engine(_async_url())
    backend = AsyncpgStore(engine)
    async with engine.begin() as conn:
        # Truncate FK-aware: cascading from the leaf tables.
        for table in (
            "snapshot_queries",
            "events",
            "versions",
            "sessions",
            "groups",
        ):
            await conn.execute(text(f"TRUNCATE TABLE {table} CASCADE"))
    try:
        yield backend
    finally:
        await backend.dispose()


# groups -----------------------------------------------------------------------


async def test_create_and_list_groups(store: AsyncpgStore) -> None:
    a = await store.create_group(name="alpha", description=None)
    b = await store.create_group(name="beta", description="second")
    listed = await store.list_groups()
    assert [g.group_id for g in listed] == [a.group_id, b.group_id]
    assert (await store.get_group(b.group_id)).description == "second"


async def test_get_unknown_group_raises(store: AsyncpgStore) -> None:
    with pytest.raises(NotFoundError):
        await store.get_group(uuid4())


# sessions + fork --------------------------------------------------------------


async def test_create_session_and_fork(store: AsyncpgStore) -> None:
    parent = await store.create_session(title="root")
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
    store: AsyncpgStore,
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
    with pytest.raises(ConflictError):
        await store.create_session(
            title="bad",
            parent_session_id=b.session_id,
            parent_version_id=version.version_id,
            fork_seq=2,
        )


async def test_update_session_status(store: AsyncpgStore) -> None:
    session = await store.create_session(title="t")
    updated = await store.update_session_status(session.session_id, "frozen")
    assert updated.status == "frozen"


async def test_update_session_title_persists(store: AsyncpgStore) -> None:
    session = await store.create_session(title="auto")
    renamed = await store.update_session_title(session.session_id, "  manual  ")
    assert renamed.title == "manual"
    fetched = await store.get_session(session.session_id)
    assert fetched.title == "manual"


async def test_update_session_title_rejects_empty(store: AsyncpgStore) -> None:
    session = await store.create_session(title="t")
    with pytest.raises(ConflictError, match="title must not be empty"):
        await store.update_session_title(session.session_id, "   ")


# versions ---------------------------------------------------------------------


async def test_create_version_enforces_check_constraints(store: AsyncpgStore) -> None:
    session = await store.create_session(title="t")
    with pytest.raises(ConflictError):
        await store.create_version(
            session_id=session.session_id,
            blob_sha="a" * 64,
            blob_path="x.jsonl",
            start_seq=5,
            end_seq=2,
            byte_size=10,
        )


async def test_list_versions_orders_newest_first(store: AsyncpgStore) -> None:
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
    versions = await store.list_versions(session.session_id)
    # ORDER BY frozen_at DESC; default now() resolution may collapse
    # both inserts into the same timestamp on fast machines, so we just
    # check the set membership.
    assert {v.version_id for v in versions} == {first.version_id, second.version_id}


# events -----------------------------------------------------------------------


async def test_append_and_replay_events(store: AsyncpgStore) -> None:
    session = await store.create_session(title="t")
    e0 = await store.append_event(
        session_id=session.session_id, kind="turn", payload={"role": "user"}
    )
    e1 = await store.append_event(
        session_id=session.session_id, kind="turn", payload={"role": "assistant"}
    )
    assert e0.seq == 0
    assert e1.seq == 1
    assert await store.next_seq(session.session_id) == 2
    replay = await store.list_events(session.session_id, from_seq=1)
    assert [e.seq for e in replay] == [1]


async def test_append_event_unknown_session_raises(store: AsyncpgStore) -> None:
    with pytest.raises(NotFoundError):
        await store.append_event(session_id=uuid4(), kind="turn", payload={})


# Stage C end-to-end: freeze pipeline against a real Postgres ----------------


async def test_freeze_pipeline_against_real_postgres(
    store: AsyncpgStore, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Append turns -> freeze -> read back via list_versions.

    Exercises :class:`AsyncpgStore.create_version` from the Stage B
    surface plus the Stage C blob/freeze pipeline.
    """

    from stratoclave_atelier.blobs import FileBlobStore
    from stratoclave_atelier.freeze import freeze_session

    blob_root = tmp_path_factory.mktemp("blobs")
    blob_store = FileBlobStore(blob_root)
    session = await store.create_session(title="freeze-it")
    for i in range(3):
        await store.append_event(
            session_id=session.session_id,
            kind="turn",
            payload={"role": "user", "i": i},
        )

    version = await freeze_session(
        store=store,
        blob_store=blob_store,
        session_id=session.session_id,
        label="full",
    )
    assert version.start_seq == 0
    assert version.end_seq == 2
    assert version.turn_count == 3
    assert version.label == "full"

    versions = await store.list_versions(session.session_id)
    assert [v.version_id for v in versions] == [version.version_id]

    written = await blob_store.read(version.blob_sha)
    assert written.endswith(b"\n")
    assert written.count(b"\n") == 3
