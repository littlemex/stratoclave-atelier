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

from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Query, status

from stratoclave_atelier.api.deps import (
    BlobStoreDep,
    ConfigDep,
    EventBusDep,
    MemoryServiceDep,
    StoreDep,
    http_conflict,
    http_not_found,
)
from stratoclave_atelier.api.schemas import (
    EventRead,
    SessionCreate,
    SessionFork,
    SessionFreeze,
    SessionRead,
    TurnAppend,
    VersionRead,
)
from stratoclave_atelier.core import ConflictError, NotFoundError
from stratoclave_atelier.freeze import freeze_session

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


def _resolve_backend(cfg: object, requested: str | None) -> str | None:
    """Validate and resolve the backend a session should run against.

    Returns the value to persist on ``Session.agent_backend``:
    * ``None`` if the caller did not specify and the server has no
      default (or if the requested value matches the default).
    * The validated backend name otherwise.

    Raises :class:`ConflictError` when the requested backend isn't in
    the operator-allowed list.
    """

    from stratoclave_atelier.config import AtelierConfig

    config = cast("AtelierConfig", cfg)
    if requested is None:
        return None
    allowed = config.resolved_backends()
    if not allowed:
        raise ConflictError(
            "no agent backends are configured on this server (set ATELIER_AGENT_BACKENDS_ALLOWED)"
        )
    if requested not in allowed:
        raise ConflictError(f"backend {requested!r} is not in the allowed list {list(allowed)}")
    return requested


@router.post(
    "",
    response_model=SessionRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_session(
    payload: SessionCreate,
    store: StoreDep,
    config: ConfigDep,
) -> SessionRead:
    try:
        backend = _resolve_backend(config, payload.agent_backend)
    except ConflictError as exc:
        raise http_conflict(exc) from exc
    try:
        session = await store.create_session(
            title=payload.title,
            group_id=payload.group_id,
            agent_backend=backend,
        )
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
    config: ConfigDep,
) -> SessionRead:
    try:
        backend = _resolve_backend(config, payload.agent_backend)
    except ConflictError as exc:
        raise http_conflict(exc) from exc
    try:
        child = await store.create_session(
            title=payload.title,
            group_id=payload.group_id,
            parent_session_id=session_id,
            parent_version_id=payload.parent_version_id,
            fork_seq=payload.fork_seq,
            agent_backend=backend,
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
    "/{session_id}/turns",
    response_model=EventRead,
    status_code=status.HTTP_201_CREATED,
)
async def append_turn(
    session_id: UUID,
    payload: TurnAppend,
    store: StoreDep,
    bus: EventBusDep,
) -> EventRead:
    """Append a single turn via HTTP (Stage F fallback to WS ingest).

    Mirrors the WebSocket ingest path: validates the session exists and
    is still ``active``, then persists a single ``events`` row with
    ``kind="turn"`` and a payload that mirrors the JSONL line.
    """

    try:
        session = await store.get_session(session_id)
    except NotFoundError as exc:
        raise http_not_found(exc) from exc
    if session.status != "active":
        raise http_conflict(ConflictError(f"session {session_id} is {session.status}, not active"))

    event = await store.append_event(
        session_id=session_id,
        kind="turn",
        payload={"kind": "turn", "role": payload.role, "content": payload.content},
    )
    await bus.publish(event)
    return EventRead.from_domain(event)


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
    memory: MemoryServiceDep,
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
            memory=memory,
        )
    except NotFoundError as exc:
        raise http_not_found(exc) from exc
    except ConflictError as exc:
        raise http_conflict(exc) from exc
    return VersionRead.from_domain(version)
