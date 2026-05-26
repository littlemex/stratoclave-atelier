"""Unit tests for :class:`EchoSnapshotResolver`."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from stratoclave_atelier.blobs import InMemoryBlobStore
from stratoclave_atelier.core import Version
from stratoclave_atelier.db import InMemoryStore
from stratoclave_atelier.snapshot_resolver import EchoSnapshotResolver


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
