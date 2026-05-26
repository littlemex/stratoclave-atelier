"""Content-addressed blob store.

Atelier persists each frozen version's JSONL bytes here. The store is
**write-once** by design: once a blob lands at its final path it is
chmoded to ``0444`` so even root has to opt in (``chmod u+w``) before
overwriting it. Identical content collapses to a single blob: a freeze
of the same JSONL twice returns the same path.

Layout
------
::

    <root>/
        sha256/
            ab/
                ab1234...cd.jsonl   # first two hex chars used as fan-out
            cd/
                cd5678...ef.jsonl

The two-character fan-out keeps any single directory below a few
thousand entries even after millions of freezes -- this is the same
trick git uses for its loose object store.

Atomicity
---------
Writes go through ``<final>.tmp.<pid>.<random>``: bytes are flushed and
``fsync``-ed, then the temp file is ``rename``-d into place. ``rename``
is atomic on POSIX, so a crash mid-write leaves either nothing or a
fully-formed blob -- never a partial one. After rename we ``chmod 0444``
so subsequent appends-to-the-same-path are explicit failures rather
than silent overwrites.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class WriteResult:
    """Outcome of writing bytes to the blob store.

    ``path`` is the absolute path on disk (or, for the in-memory store,
    a stable virtual path). ``byte_size`` is ``len(payload)`` so callers
    do not need to recompute it. ``existed`` is true iff the blob was
    already present, in which case the bytes on disk are identical and
    no write happened.
    """

    sha256: str
    path: str
    byte_size: int
    existed: bool


class BlobStore(Protocol):
    """Read/write surface for the content-addressed blob store."""

    async def write(self, payload: bytes) -> WriteResult: ...

    async def read(self, sha256: str) -> bytes: ...

    async def exists(self, sha256: str) -> bool: ...


class FileBlobStore(BlobStore):
    """Filesystem-backed content-addressed store.

    The constructor creates ``root`` if missing but does not pre-create
    the ``sha256/<aa>`` fan-out directories; those are made on first
    write into each prefix. All blocking I/O runs in
    :func:`asyncio.to_thread` so the event loop is never stalled by a
    slow filesystem.
    """

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    def _path_for(self, sha256: str) -> Path:
        if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
            raise ValueError(f"sha256 must be 64 hex chars, got {sha256!r}")
        return self._root / "sha256" / sha256[:2] / f"{sha256}.jsonl"

    async def write(self, payload: bytes) -> WriteResult:
        digest = hashlib.sha256(payload).hexdigest()
        final = self._path_for(digest)
        return await asyncio.to_thread(self._write_sync, payload, digest, final)

    def _write_sync(self, payload: bytes, digest: str, final: Path) -> WriteResult:
        if final.exists():
            return WriteResult(
                sha256=digest,
                path=str(final),
                byte_size=len(payload),
                existed=True,
            )
        final.parent.mkdir(parents=True, exist_ok=True)
        # Random suffix prevents collisions if two freezes for the same
        # digest race in. The loser will see ``final.exists()`` after
        # rename and is harmless because both wrote identical bytes.
        tmp = final.with_name(f"{final.name}.tmp.{os.getpid()}.{secrets.token_hex(4)}")
        try:
            with tmp.open("xb") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, final)
            os.chmod(final, 0o444)
        except FileExistsError:
            # Race: another writer materialised the same digest between
            # the existence check and our open. Drop our temp and fall
            # through; the bytes are identical by content-addressing.
            tmp.unlink(missing_ok=True)
            return WriteResult(
                sha256=digest,
                path=str(final),
                byte_size=len(payload),
                existed=True,
            )
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
        return WriteResult(
            sha256=digest,
            path=str(final),
            byte_size=len(payload),
            existed=False,
        )

    async def read(self, sha256: str) -> bytes:
        path = self._path_for(sha256)
        return await asyncio.to_thread(path.read_bytes)

    async def exists(self, sha256: str) -> bool:
        path = self._path_for(sha256)
        return await asyncio.to_thread(path.exists)


class InMemoryBlobStore(BlobStore):
    """Dict-backed BlobStore for unit tests.

    Mimics :class:`FileBlobStore` semantics (write-once, idempotent
    re-write) without touching the filesystem. The synthetic ``path`` is
    ``mem://sha256/<aa>/<full>.jsonl`` so test assertions can still
    pattern-match on it.
    """

    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}

    async def write(self, payload: bytes) -> WriteResult:
        digest = hashlib.sha256(payload).hexdigest()
        existed = digest in self._store
        if not existed:
            self._store[digest] = payload
        return WriteResult(
            sha256=digest,
            path=f"mem://sha256/{digest[:2]}/{digest}.jsonl",
            byte_size=len(payload),
            existed=existed,
        )

    async def read(self, sha256: str) -> bytes:
        try:
            return self._store[sha256]
        except KeyError as exc:
            raise FileNotFoundError(sha256) from exc

    async def exists(self, sha256: str) -> bool:
        return sha256 in self._store
