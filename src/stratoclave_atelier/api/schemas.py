"""Pydantic request/response schemas for the atelier REST API.

The schemas mirror the domain dataclasses in
:mod:`stratoclave_atelier.core.types` but remain a separate layer so we
can evolve the wire format independently. Each ``*Read`` model has a
``from_domain`` classmethod that accepts the corresponding domain
object.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from stratoclave_atelier.core import (
    Event,
    EventKind,
    ForkGraphEdge,
    ForkGraphNode,
    ForkGraphVersion,
    Group,
    Session,
    SessionStatus,
    SnapshotQuery,
    Version,
)

# Groups -----------------------------------------------------------------------


class GroupCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


class GroupRead(BaseModel):
    group_id: UUID
    name: str
    description: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, group: Group) -> GroupRead:
        return cls(
            group_id=group.group_id,
            name=group.name,
            description=group.description,
            created_at=group.created_at,
            updated_at=group.updated_at,
        )


# Sessions ---------------------------------------------------------------------


class SessionCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    group_id: UUID | None = None


class SessionRead(BaseModel):
    session_id: UUID
    group_id: UUID | None
    title: str
    parent_session_id: UUID | None
    parent_version_id: UUID | None
    fork_seq: int | None
    status: SessionStatus
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, session: Session) -> SessionRead:
        return cls(
            session_id=session.session_id,
            group_id=session.group_id,
            title=session.title,
            parent_session_id=session.parent_session_id,
            parent_version_id=session.parent_version_id,
            fork_seq=session.fork_seq,
            status=session.status,
            created_at=session.created_at,
            updated_at=session.updated_at,
        )


class SessionFork(BaseModel):
    """Fork a child session from a frozen version + turn.

    ``parent_version_id`` must reference a version whose ``session_id``
    matches the path parameter; ``fork_seq`` must lie in
    ``[start_seq, end_seq]`` of that version.
    """

    title: str = Field(..., min_length=1, max_length=200)
    parent_version_id: UUID
    fork_seq: int = Field(..., ge=0)
    group_id: UUID | None = None


# Versions ---------------------------------------------------------------------


class VersionRead(BaseModel):
    version_id: UUID
    session_id: UUID
    blob_sha: str
    blob_path: str
    turn_count: int
    start_seq: int
    end_seq: int
    byte_size: int
    label: str | None
    frozen_at: datetime

    @classmethod
    def from_domain(cls, version: Version) -> VersionRead:
        return cls(
            version_id=version.version_id,
            session_id=version.session_id,
            blob_sha=version.blob_sha,
            blob_path=version.blob_path,
            turn_count=version.turn_count,
            start_seq=version.start_seq,
            end_seq=version.end_seq,
            byte_size=version.byte_size,
            label=version.label,
            frozen_at=version.frozen_at,
        )


# Turns (HTTP append, alternative to WS ingest) -------------------------------


class TurnAppend(BaseModel):
    """Append a single turn to a session via HTTP.

    Stage F adds this fallback so the CLI ``session send-turn`` can drive
    a session without opening a WebSocket. The persisted state is
    identical to what the ``/ingest`` socket would produce: one row in
    ``events`` with ``kind="turn"`` and a payload mirroring the JSONL
    line.
    """

    role: str = Field(..., min_length=1, max_length=64)
    content: str = Field(..., max_length=200_000)


# Freeze -----------------------------------------------------------------------


class SessionFreeze(BaseModel):
    """Freeze a contiguous turn range into an immutable Version.

    Both ``start_seq`` and ``end_seq`` are optional; when omitted the
    handler interprets them as "from the first turn ever appended" and
    "up to the latest turn", respectively. ``label`` is a free-form
    annotation surfaced by the UI ("baseline", "after refactor", ...);
    it is not used for content addressing.
    """

    start_seq: int | None = Field(default=None, ge=0)
    end_seq: int | None = Field(default=None, ge=0)
    label: str | None = Field(default=None, max_length=200)


# Events -----------------------------------------------------------------------


class EventRead(BaseModel):
    event_id: UUID
    session_id: UUID
    seq: int
    kind: EventKind
    payload: dict[str, Any]
    created_at: datetime

    @classmethod
    def from_domain(cls, event: Event) -> EventRead:
        return cls(
            event_id=event.event_id,
            session_id=event.session_id,
            seq=event.seq,
            kind=event.kind,
            payload=event.payload,
            created_at=event.created_at,
        )


# Snapshot queries ------------------------------------------------------------


class SnapshotQueryCreate(BaseModel):
    """A cross-session question against a frozen :class:`Version`.

    The handler resolves the question via the registered
    :class:`SnapshotResolver`, persists both the query and the response,
    and returns the audit row.
    """

    target_version_id: UUID
    query: str = Field(..., min_length=1, max_length=4000)


class SnapshotQueryRead(BaseModel):
    query_id: UUID
    source_session_id: UUID
    target_version_id: UUID
    query: str
    response: str | None
    created_at: datetime

    @classmethod
    def from_domain(cls, row: SnapshotQuery) -> SnapshotQueryRead:
        return cls(
            query_id=row.query_id,
            source_session_id=row.source_session_id,
            target_version_id=row.target_version_id,
            query=row.query,
            response=row.response,
            created_at=row.created_at,
        )


# Fork graph ------------------------------------------------------------------


class ForkGraphVersionRead(BaseModel):
    version_id: UUID
    label: str | None
    start_seq: int
    end_seq: int
    turn_count: int

    @classmethod
    def from_domain(cls, v: ForkGraphVersion) -> ForkGraphVersionRead:
        return cls(
            version_id=v.version_id,
            label=v.label,
            start_seq=v.start_seq,
            end_seq=v.end_seq,
            turn_count=v.turn_count,
        )


class ForkGraphNodeRead(BaseModel):
    session_id: UUID
    title: str
    status: SessionStatus
    parent_session_id: UUID | None
    parent_version_id: UUID | None
    fork_seq: int | None
    versions: list[ForkGraphVersionRead]

    @classmethod
    def from_domain(cls, node: ForkGraphNode) -> ForkGraphNodeRead:
        return cls(
            session_id=node.session_id,
            title=node.title,
            status=node.status,
            parent_session_id=node.parent_session_id,
            parent_version_id=node.parent_version_id,
            fork_seq=node.fork_seq,
            versions=[ForkGraphVersionRead.from_domain(v) for v in node.versions],
        )


class ForkGraphEdgeRead(BaseModel):
    parent_session_id: UUID
    child_session_id: UUID
    via_version_id: UUID
    fork_seq: int

    @classmethod
    def from_domain(cls, edge: ForkGraphEdge) -> ForkGraphEdgeRead:
        return cls(
            parent_session_id=edge.parent_session_id,
            child_session_id=edge.child_session_id,
            via_version_id=edge.via_version_id,
            fork_seq=edge.fork_seq,
        )


class ForkGraphResponse(BaseModel):
    nodes: list[ForkGraphNodeRead]
    edges: list[ForkGraphEdgeRead]
