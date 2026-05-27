"""Unit tests for snapshot resolvers (Echo + Distill)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from stratoclave_atelier.blobs import InMemoryBlobStore
from stratoclave_atelier.core import Version
from stratoclave_atelier.db import InMemoryStore
from stratoclave_atelier.snapshot_resolver import (
    DistillSnapshotResolver,
    EchoSnapshotResolver,
)


def _make_version(label: str | None) -> Version:
    return Version(
        version_id=uuid4(),
        session_id=uuid4(),
        blob_sha="a" * 64,
        blob_path="x.jsonl",
        turn_count=4,
        start_seq=0,
        end_seq=3,
        byte_size=10,
        label=label,
        frozen_at=datetime.now(tz=UTC),
    )


@pytest.mark.asyncio
async def test_echo_resolver_includes_label_and_turn_count() -> None:
    resolver = EchoSnapshotResolver()
    store = InMemoryStore()
    blob_store = InMemoryBlobStore()
    version = _make_version(label="baseline")

    response = await resolver.resolve(
        store=store, blob_store=blob_store, version=version, query="why?"
    )
    assert "version=baseline" in response
    assert "turns=4" in response
    assert "why?" in response


@pytest.mark.asyncio
async def test_echo_resolver_handles_missing_label() -> None:
    resolver = EchoSnapshotResolver()
    store = InMemoryStore()
    blob_store = InMemoryBlobStore()
    version = _make_version(label=None)

    response = await resolver.resolve(
        store=store, blob_store=blob_store, version=version, query="q"
    )
    assert "<unlabeled>" in response


# ---------------------------------------------------------------------------
# DistillSnapshotResolver
# ---------------------------------------------------------------------------


@dataclass
class _StubLearning:
    rule: str


@dataclass
class _StubHit:
    learning: _StubLearning


@dataclass
class _StubGap:
    topic: str


@dataclass
class _StubRetrievalResult:
    canonical: tuple[_StubHit, ...] = ()
    emerging: tuple[_StubHit, ...] = ()
    conflicts: tuple[Any, ...] = ()
    gaps: tuple[_StubGap, ...] = ()


class _StubRetriever:
    def __init__(self, *, result: _StubRetrievalResult, fail: bool = False) -> None:
        self._result = result
        self._fail = fail
        self.calls: list[str] = []

    async def retrieve(self, query: str) -> _StubRetrievalResult:
        self.calls.append(query)
        if self._fail:
            raise RuntimeError("boom")
        return self._result


class _StubPool:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_distill_resolver_renders_header_query_roles_and_memory() -> None:
    pool = _StubPool()
    retriever = _StubRetriever(
        result=_StubRetrievalResult(
            canonical=(_StubHit(_StubLearning(rule="prefer claude_code")),),
            emerging=(_StubHit(_StubLearning(rule="kiro_code is faster")),),
            gaps=(_StubGap(topic="cost"),),
        ),
    )
    resolver = DistillSnapshotResolver(pool=pool, retriever=retriever)

    blob_store = InMemoryBlobStore()
    payload = (
        json.dumps({"role": "user", "content": "hi"})
        + "\n"
        + json.dumps({"role": "assistant", "content": "hello"})
        + "\n"
        + json.dumps({"role": "user", "content": "?"})
    ).encode("utf-8")
    write = await blob_store.write(payload)
    version = Version(
        version_id=uuid4(),
        session_id=uuid4(),
        blob_sha=write.sha256,
        blob_path=write.path,
        turn_count=3,
        start_seq=0,
        end_seq=2,
        byte_size=len(payload),
        label="baseline",
        frozen_at=datetime.now(tz=UTC),
    )

    response = await resolver.resolve(
        store=InMemoryStore(),
        blob_store=blob_store,
        version=version,
        query="which backend?",
    )

    assert response.startswith("[distill] version=baseline turns=3")
    assert "query: which backend?" in response
    assert "turn-roles: assistant=1, user=2" in response
    assert "memory:\n[canonical] prefer claude_code" in response
    assert "[emerging] kiro_code is faster" in response
    assert "[gap] cost" in response
    assert retriever.calls == ["which backend?"]


@pytest.mark.asyncio
async def test_distill_resolver_falls_back_to_none_on_empty_retrieval() -> None:
    resolver = DistillSnapshotResolver(
        pool=_StubPool(),
        retriever=_StubRetriever(result=_StubRetrievalResult()),
    )
    blob_store = InMemoryBlobStore()
    write = await blob_store.write(b"")
    version = Version(
        version_id=uuid4(),
        session_id=uuid4(),
        blob_sha=write.sha256,
        blob_path=write.path,
        turn_count=0,
        start_seq=0,
        end_seq=0,
        byte_size=0,
        label=None,
        frozen_at=datetime.now(tz=UTC),
    )

    response = await resolver.resolve(
        store=InMemoryStore(),
        blob_store=blob_store,
        version=version,
        query="anything",
    )
    assert "memory: <none>" in response
    assert "turn-roles: <empty>" in response
    assert "version=<unlabeled>" in response


@pytest.mark.asyncio
async def test_distill_resolver_swallows_retriever_exceptions() -> None:
    resolver = DistillSnapshotResolver(
        pool=_StubPool(),
        retriever=_StubRetriever(result=_StubRetrievalResult(), fail=True),
    )
    blob_store = InMemoryBlobStore()
    write = await blob_store.write(b'{"role": "user", "content": "hi"}\n')
    version = Version(
        version_id=uuid4(),
        session_id=uuid4(),
        blob_sha=write.sha256,
        blob_path=write.path,
        turn_count=1,
        start_seq=0,
        end_seq=0,
        byte_size=write.byte_size,
        label="x",
        frozen_at=datetime.now(tz=UTC),
    )

    response = await resolver.resolve(
        store=InMemoryStore(),
        blob_store=blob_store,
        version=version,
        query="will fail",
    )
    assert "memory: <none>" in response
    # turn-roles still rendered from the JSONL even though retrieval failed.
    assert "turn-roles: user=1" in response


@pytest.mark.asyncio
async def test_distill_resolver_handles_missing_blob() -> None:
    """If the JSONL is unreachable we still produce a deterministic answer."""

    resolver = DistillSnapshotResolver(
        pool=_StubPool(),
        retriever=_StubRetriever(result=_StubRetrievalResult()),
    )
    blob_store = InMemoryBlobStore()
    version = Version(
        version_id=uuid4(),
        session_id=uuid4(),
        blob_sha="b" * 64,  # not present in the in-memory store
        blob_path="missing",
        turn_count=2,
        start_seq=0,
        end_seq=1,
        byte_size=0,
        label=None,
        frozen_at=datetime.now(tz=UTC),
    )

    response = await resolver.resolve(
        store=InMemoryStore(),
        blob_store=blob_store,
        version=version,
        query="q",
    )
    assert "[distill] version=<unlabeled>" in response
    assert "turn-roles: <empty>" in response


@pytest.mark.asyncio
async def test_distill_resolver_aclose_closes_pool_once() -> None:
    pool = _StubPool()
    resolver = DistillSnapshotResolver(
        pool=pool,
        retriever=_StubRetriever(result=_StubRetrievalResult()),
    )
    await resolver.aclose()
    await resolver.aclose()  # idempotent
    assert pool.closed is True
