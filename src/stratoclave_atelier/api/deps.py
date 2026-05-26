"""Dependency-injection helpers for the FastAPI app.

The :class:`Store` is attached to ``app.state.store`` by
:func:`stratoclave_atelier.server.create_app` (or by tests that build an
app around an :class:`InMemoryStore`). Route handlers use
:func:`get_store` so the handler signature stays free of FastAPI globals
and remains independently testable.
"""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import Depends, HTTPException, Request, status

from stratoclave_atelier.blobs import BlobStore
from stratoclave_atelier.core import ConflictError, NotFoundError
from stratoclave_atelier.db import Store
from stratoclave_atelier.snapshot_resolver import SnapshotResolver


def get_store(request: Request) -> Store:
    """Return the :class:`Store` attached to the FastAPI app."""

    store = getattr(request.app.state, "store", None)
    if store is None:  # pragma: no cover -- developer error if hit
        raise RuntimeError(
            "Store is not configured on app.state.store; "
            "build the app via create_app() with a Store"
        )
    return cast(Store, store)


def get_blob_store(request: Request) -> BlobStore:
    """Return the :class:`BlobStore` attached to the FastAPI app.

    Stage C wires this in :func:`stratoclave_atelier.server.create_app`
    via the lifespan callback. Tests can pre-populate
    ``app.state.blob_store`` (with :class:`InMemoryBlobStore`) before
    issuing requests.
    """

    blob_store = getattr(request.app.state, "blob_store", None)
    if blob_store is None:  # pragma: no cover -- developer error if hit
        raise RuntimeError(
            "BlobStore is not configured on app.state.blob_store; "
            "build the app via create_app() so the lifespan can attach one"
        )
    return cast(BlobStore, blob_store)


def get_snapshot_resolver(request: Request) -> SnapshotResolver:
    """Return the :class:`SnapshotResolver` attached to the FastAPI app.

    Production wires :class:`EchoSnapshotResolver` by default; tests can
    inject a stub that records resolve() calls.
    """

    resolver = getattr(request.app.state, "snapshot_resolver", None)
    if resolver is None:  # pragma: no cover -- developer error if hit
        raise RuntimeError(
            "SnapshotResolver is not configured on app.state.snapshot_resolver; "
            "build the app via create_app() so the lifespan can attach one"
        )
    return cast(SnapshotResolver, resolver)


StoreDep = Annotated[Store, Depends(get_store)]
BlobStoreDep = Annotated[BlobStore, Depends(get_blob_store)]
SnapshotResolverDep = Annotated[SnapshotResolver, Depends(get_snapshot_resolver)]


def http_not_found(error: NotFoundError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error))


def http_conflict(error: ConflictError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))
