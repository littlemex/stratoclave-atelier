"""Unit tests for the content-addressed blob store."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

import pytest

from stratoclave_atelier.blobs import (
    FileBlobStore,
    InMemoryBlobStore,
)


@pytest.mark.asyncio
async def test_in_memory_write_returns_correct_digest_and_size() -> None:
    store = InMemoryBlobStore()
    payload = b'{"role":"user","content":"hi"}\n'
    result = await store.write(payload)
    assert result.sha256 == hashlib.sha256(payload).hexdigest()
    assert result.byte_size == len(payload)
    assert result.existed is False
    assert result.path.startswith("mem://sha256/")


@pytest.mark.asyncio
async def test_in_memory_idempotent_write() -> None:
    store = InMemoryBlobStore()
    payload = b"x"
    first = await store.write(payload)
    second = await store.write(payload)
    assert first.sha256 == second.sha256
    assert first.path == second.path
    assert second.existed is True


@pytest.mark.asyncio
async def test_in_memory_read_round_trip() -> None:
    store = InMemoryBlobStore()
    payload = b"abc"
    write = await store.write(payload)
    assert await store.read(write.sha256) == payload
    assert await store.exists(write.sha256) is True


@pytest.mark.asyncio
async def test_in_memory_read_missing_raises() -> None:
    store = InMemoryBlobStore()
    with pytest.raises(FileNotFoundError):
        await store.read("a" * 64)


@pytest.mark.asyncio
async def test_file_store_writes_with_fan_out_directory(tmp_path: Path) -> None:
    store = FileBlobStore(tmp_path)
    payload = b"hello\n"
    result = await store.write(payload)
    digest = hashlib.sha256(payload).hexdigest()
    expected = tmp_path / "sha256" / digest[:2] / f"{digest}.jsonl"
    assert Path(result.path) == expected
    assert expected.read_bytes() == payload


@pytest.mark.asyncio
async def test_file_store_writes_are_chmod_0444(tmp_path: Path) -> None:
    store = FileBlobStore(tmp_path)
    result = await store.write(b"data")
    mode = stat.S_IMODE(os.stat(result.path).st_mode)
    assert mode == 0o444


@pytest.mark.asyncio
async def test_file_store_idempotent_on_identical_payload(tmp_path: Path) -> None:
    store = FileBlobStore(tmp_path)
    payload = b"identical"
    first = await store.write(payload)
    second = await store.write(payload)
    assert first.path == second.path
    assert first.sha256 == second.sha256
    assert second.existed is True


@pytest.mark.asyncio
async def test_file_store_distinct_payloads_distinct_paths(tmp_path: Path) -> None:
    store = FileBlobStore(tmp_path)
    one = await store.write(b"one")
    two = await store.write(b"two")
    assert one.sha256 != two.sha256
    assert one.path != two.path


@pytest.mark.asyncio
async def test_file_store_read_round_trip(tmp_path: Path) -> None:
    store = FileBlobStore(tmp_path)
    payload = b"round-trip\n"
    write = await store.write(payload)
    assert await store.read(write.sha256) == payload
    assert await store.exists(write.sha256) is True


@pytest.mark.asyncio
async def test_file_store_rejects_invalid_digest_format(tmp_path: Path) -> None:
    store = FileBlobStore(tmp_path)
    with pytest.raises(ValueError):
        await store.read("not-hex")
    with pytest.raises(ValueError):
        await store.exists("a" * 63)
