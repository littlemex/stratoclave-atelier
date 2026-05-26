"""Content-addressed blob store for stratoclave-atelier.

Stage C exposes :class:`BlobStore` as the only public surface. The
runtime implementation, :class:`FileBlobStore`, writes to a directory on
disk; tests can substitute :class:`InMemoryBlobStore` to avoid touching
the filesystem.
"""

from stratoclave_atelier.blobs.store import (
    BlobStore,
    FileBlobStore,
    InMemoryBlobStore,
    WriteResult,
)

__all__ = [
    "BlobStore",
    "FileBlobStore",
    "InMemoryBlobStore",
    "WriteResult",
]
