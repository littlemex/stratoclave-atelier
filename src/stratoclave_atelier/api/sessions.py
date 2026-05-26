"""``/api/sessions`` REST router.

Stage B exposes:

* ``POST /api/sessions`` -- create a root session (no parent).
* ``GET  /api/sessions`` -- list sessions, optionally filtered by group.
* ``GET  /api/sessions/{id}`` -- fetch one session.
* ``POST /api/sessions/{id}/fork`` -- fork from a frozen version + turn.
* ``GET  /api/sessions/{id}/versions`` -- list versions for a session.

Stage C adds:

* ``POST /api/sessions/{id}/freeze`` -- freeze a turn range into an
  immutable :class:`Version` backed by the content-addressed blob
  store.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, status

from stratoclave_atelier.api.deps import (
    BlobStoreDep,
    StoreDep,
    http_conflict,
    http_not_found,
)
from stratoclave_atelier.api.schemas import (
    SessionCreate,
    SessionFork,
    SessionFreeze,
    SessionRead,
    VersionRead,
)
from stratoclave_atelier.core import ConflictError, NotFoundError
from stratoclave_atelier.freeze import freeze_session

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


@router.post(
    "",
    response_model=SessionRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_session(payload: SessionCreate, store: StoreDep) -> SessionRead:
    try:
        session = await store.create_session(title=payload.title, group_id=payload.group_id)
    except NotFoundError as exc:
        raise http_not_found(exc) from exc
    return SessionRead.from_domain(session)


@router.get("", response_model=list[SessionRead])
async def list_sessions(
    store: StoreDep,
    group_id: Annotated[UUID | None, Query()] = None,
) -> list[SessionRead]:
    sessions = await store.list_sessions(group_id=group_id)
    return [SessionRead.from_domain(s) for s in sessions]


@router.get("/{session_id}", response_model=SessionRead)
async def get_session(session_id: UUID, store: StoreDep) -> SessionRead:
    try:
        session = await store.get_session(session_id)
    except NotFoundError as exc:
        raise http_not_found(exc) from exc
    return SessionRead.from_domain(session)


@router.post(
    "/{session_id}/fork",
    response_model=SessionRead,
    status_code=status.HTTP_201_CREATED,
)
async def fork_session(
    session_id: UUID,
    payload: SessionFork,
    store: StoreDep,
) -> SessionRead:
    try:
        child = await store.create_session(
            title=payload.title,
            group_id=payload.group_id,
            parent_session_id=session_id,
            parent_version_id=payload.parent_version_id,
            fork_seq=payload.fork_seq,
        )
    except NotFoundError as exc:
        raise http_not_found(exc) from exc
    except ConflictError as exc:
        raise http_conflict(exc) from exc
    return SessionRead.from_domain(child)


@router.get("/{session_id}/versions", response_model=list[VersionRead])
async def list_versions(session_id: UUID, store: StoreDep) -> list[VersionRead]:
    try:
        versions = await store.list_versions(session_id)
    except NotFoundError as exc:
        raise http_not_found(exc) from exc
    return [VersionRead.from_domain(v) for v in versions]


@router.post(
    "/{session_id}/freeze",
    response_model=VersionRead,
    status_code=status.HTTP_201_CREATED,
)
async def freeze(
    session_id: UUID,
    payload: SessionFreeze,
    store: StoreDep,
    blob_store: BlobStoreDep,
) -> VersionRead:
    """Freeze a turn range into an immutable Version.

    With an empty body the entire session is frozen (Stage C requirement
    "freeze the whole session"); ``start_seq`` and ``end_seq`` narrow
    the range for per-turn or "freeze from this turn" semantics.
    """

    try:
        version = await freeze_session(
            store=store,
            blob_store=blob_store,
            session_id=session_id,
            start_seq=payload.start_seq,
            end_seq=payload.end_seq,
            label=payload.label,
        )
    except NotFoundError as exc:
        raise http_not_found(exc) from exc
    except ConflictError as exc:
        raise http_conflict(exc) from exc
    return VersionRead.from_domain(version)
