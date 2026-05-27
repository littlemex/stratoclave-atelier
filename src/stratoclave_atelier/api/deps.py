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

from stratoclave_atelier.auto_namer import AutoNamer
from stratoclave_atelier.blobs import BlobStore
from stratoclave_atelier.config import AtelierConfig
from stratoclave_atelier.core import ConflictError, NotFoundError
from stratoclave_atelier.db import Store
from stratoclave_atelier.events_bus import EventBus
from stratoclave_atelier.memory import MemoryService
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


def get_event_bus(request: Request) -> EventBus:
    """Return the :class:`EventBus` attached to the FastAPI app.

    Stage G adds a process-local bus for SSE live broadcast. Routers
    that append events should publish to the bus so subscribers receive
    them in real time.
    """

    bus = getattr(request.app.state, "event_bus", None)
    if bus is None:  # pragma: no cover -- developer error if hit
        raise RuntimeError(
            "EventBus is not configured on app.state.event_bus; "
            "build the app via create_app() so the lifespan can attach one"
        )
    return cast(EventBus, bus)


def get_memory_service(request: Request) -> MemoryService:
    """Return the :class:`MemoryService` attached to the FastAPI app.

    Stage G-4 adds the cross-session memory layer. The default
    :class:`NoopMemoryService` is wired when distill is disabled, so
    handlers can always depend on a non-None object.
    """

    memory = getattr(request.app.state, "memory_service", None)
    if memory is None:  # pragma: no cover -- developer error if hit
        raise RuntimeError(
            "MemoryService is not configured on app.state.memory_service; "
            "build the app via create_app() so the lifespan can attach one"
        )
    return cast(MemoryService, memory)


def get_auto_namer(request: Request) -> AutoNamer:
    """Return the :class:`AutoNamer` attached to the FastAPI app.

    Stage J wires this in :func:`stratoclave_atelier.server.create_app`
    via the lifespan callback. The default :class:`NoopAutoNamer` is
    selected when no agent backend is configured, so handlers can
    always depend on a non-None object.
    """

    namer = getattr(request.app.state, "auto_namer", None)
    if namer is None:  # pragma: no cover -- developer error if hit
        raise RuntimeError(
            "AutoNamer is not configured on app.state.auto_namer; "
            "build the app via create_app() so the lifespan can attach one"
        )
    return cast(AutoNamer, namer)


def get_config(request: Request) -> AtelierConfig:
    """Return the :class:`AtelierConfig` attached to the FastAPI app."""

    cfg = getattr(request.app.state, "config", None)
    if cfg is None:  # pragma: no cover -- developer error if hit
        raise RuntimeError(
            "AtelierConfig is not configured on app.state.config; "
            "build the app via create_app() with a config"
        )
    return cast(AtelierConfig, cfg)


StoreDep = Annotated[Store, Depends(get_store)]
BlobStoreDep = Annotated[BlobStore, Depends(get_blob_store)]
SnapshotResolverDep = Annotated[SnapshotResolver, Depends(get_snapshot_resolver)]
EventBusDep = Annotated[EventBus, Depends(get_event_bus)]
MemoryServiceDep = Annotated[MemoryService, Depends(get_memory_service)]
AutoNamerDep = Annotated[AutoNamer, Depends(get_auto_namer)]
ConfigDep = Annotated[AtelierConfig, Depends(get_config)]


def http_not_found(error: NotFoundError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error))


def http_conflict(error: ConflictError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))
