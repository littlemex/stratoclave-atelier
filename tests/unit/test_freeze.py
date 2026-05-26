"""Unit tests for the freeze pipeline (events -> JSONL -> BlobStore -> Version)."""

from __future__ import annotations

import hashlib

import pytest

from stratoclave_atelier.blobs import InMemoryBlobStore
from stratoclave_atelier.core import ConflictError, NotFoundError
from stratoclave_atelier.db import InMemoryStore
from stratoclave_atelier.freeze import freeze_session, serialise_jsonl


@pytest.mark.asyncio
async def test_serialise_empty_events_returns_empty_bytes() -> None:
    assert serialise_jsonl([]) == b""


@pytest.mark.asyncio
async def test_freeze_writes_blob_and_creates_version() -> None:
    store = InMemoryStore()
    blob = InMemoryBlobStore()
    session = await store.create_session(title="s")
    await store.append_event(
        session_id=session.session_id,
        kind="turn",
        payload={"role": "user", "content": "hi"},
    )
    await store.append_event(
        session_id=session.session_id,
        kind="turn",
        payload={"role": "assistant", "content": "hello"},
    )

    version = await freeze_session(
        store=store,
        blob_store=blob,
        session_id=session.session_id,
        label="baseline",
    )

    assert version.session_id == session.session_id
    assert version.start_seq == 0
    assert version.end_seq == 1
    assert version.turn_count == 2
    assert version.label == "baseline"
    blob_bytes = await blob.read(version.blob_sha)
    assert version.blob_sha == hashlib.sha256(blob_bytes).hexdigest()
    assert blob_bytes.endswith(b"\n")
    assert b'"role":"assistant"' in blob_bytes


@pytest.mark.asyncio
async def test_freeze_is_content_addressed_idempotent() -> None:
    store = InMemoryStore()
    blob = InMemoryBlobStore()
    session = await store.create_session(title="s")
    await store.append_event(
        session_id=session.session_id,
        kind="turn",
        payload={"role": "user", "content": "x"},
    )

    first = await freeze_session(
        store=store,
        blob_store=blob,
        session_id=session.session_id,
    )
    second = await freeze_session(
        store=store,
        blob_store=blob,
        session_id=session.session_id,
    )

    # Same JSONL content -> same SHA-256 even though it is two distinct
    # Version rows.
    assert first.blob_sha == second.blob_sha
    assert first.version_id != second.version_id


@pytest.mark.asyncio
async def test_freeze_range_subselects_turns() -> None:
    store = InMemoryStore()
    blob = InMemoryBlobStore()
    session = await store.create_session(title="s")
    for i in range(5):
        await store.append_event(
            session_id=session.session_id,
            kind="turn",
            payload={"i": i},
        )

    version = await freeze_session(
        store=store,
        blob_store=blob,
        session_id=session.session_id,
        start_seq=1,
        end_seq=3,
    )

    assert version.start_seq == 1
    assert version.end_seq == 3
    assert version.turn_count == 3


@pytest.mark.asyncio
async def test_freeze_skips_non_turn_events() -> None:
    store = InMemoryStore()
    blob = InMemoryBlobStore()
    session = await store.create_session(title="s")
    await store.append_event(session_id=session.session_id, kind="turn", payload={"i": 0})
    await store.append_event(
        session_id=session.session_id, kind="system", payload={"note": "skip me"}
    )
    await store.append_event(session_id=session.session_id, kind="turn", payload={"i": 2})

    version = await freeze_session(
        store=store,
        blob_store=blob,
        session_id=session.session_id,
    )

    blob_bytes = await blob.read(version.blob_sha)
    assert b"skip me" not in blob_bytes
    assert blob_bytes.count(b"\n") == 2  # 2 turn events


@pytest.mark.asyncio
async def test_freeze_empty_session_raises_conflict() -> None:
    store = InMemoryStore()
    blob = InMemoryBlobStore()
    session = await store.create_session(title="s")
    with pytest.raises(ConflictError):
        await freeze_session(
            store=store,
            blob_store=blob,
            session_id=session.session_id,
        )


@pytest.mark.asyncio
async def test_freeze_missing_session_raises_not_found() -> None:
    from uuid import uuid4

    store = InMemoryStore()
    blob = InMemoryBlobStore()
    with pytest.raises(NotFoundError):
        await freeze_session(
            store=store,
            blob_store=blob,
            session_id=uuid4(),
        )


@pytest.mark.asyncio
async def test_freeze_inverted_range_raises_conflict() -> None:
    store = InMemoryStore()
    blob = InMemoryBlobStore()
    session = await store.create_session(title="s")
    await store.append_event(session_id=session.session_id, kind="turn", payload={"i": 0})
    with pytest.raises(ConflictError):
        await freeze_session(
            store=store,
            blob_store=blob,
            session_id=session.session_id,
            start_seq=5,
            end_seq=2,
        )
