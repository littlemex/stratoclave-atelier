"""``/api/sessions`` REST router.

Stage B exposes:

* ``POST /api/sessions`` -- create a root session (no parent).
* ``GET  /api/sessions`` -- list sessions, optionally filtered by group.
* ``GET  /api/sessions/{id}`` -- fetch one session.
* ``POST /api/sessions/{id}/fork`` -- fork from a frozen version + turn.
* ``GET  /api/sessions/{id}/versions`` -- list versions for a session.

Freeze (write a Version) lands in Stage C alongside the blob store; for
now the only way to land Versions is by writing to the database
directly (e.g. integration tests).
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, status

from stratoclave_atelier.api.deps import StoreDep, http_conflict, http_not_found
from stratoclave_atelier.api.schemas import (
    SessionCreate,
    SessionFork,
    SessionRead,
    VersionRead,
)
from stratoclave_atelier.core import ConflictError, NotFoundError

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
