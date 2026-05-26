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
    Group,
    Session,
    SessionStatus,
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
