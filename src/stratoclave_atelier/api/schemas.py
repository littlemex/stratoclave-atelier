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


_HEX_COLOR_PATTERN = r"^#[0-9A-Fa-f]{6}$"


class GroupCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    color: str = Field(..., pattern=_HEX_COLOR_PATTERN)


class GroupUpdate(BaseModel):
    """Partial update for a group.

    Stage L lets the operator rename / recolour a group from the Fork
    DAG sidebar after the fact. Both fields are optional; an empty body
    is rejected by the handler so a misclick does not silently no-op.
    """

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    color: str | None = Field(default=None, pattern=_HEX_COLOR_PATTERN)


class GroupRead(BaseModel):
    group_id: UUID
    name: str
    description: str | None
    color: str
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, group: Group) -> GroupRead:
        return cls(
            group_id=group.group_id,
            name=group.name,
            description=group.description,
            color=group.color,
            created_at=group.created_at,
            updated_at=group.updated_at,
        )


class SessionGroupAssign(BaseModel):
    """Move a session into a group, or remove it from any group.

    ``group_id = None`` removes the session from its current group; a
    UUID assigns it. Stage L only accepts assignments on root sessions
    (``parent_session_id IS NULL``) -- forks inherit their root's
    grouping by way of the DAG and should not be re-grouped
    independently.
    """

    group_id: UUID | None = None


# Sessions ---------------------------------------------------------------------


class SessionCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    group_id: UUID | None = None
    agent_backend: str | None = Field(
        default=None,
        description=(
            "Loom backend to use for this session "
            "(claude_code / kiro_code / mock). "
            "When omitted the server falls back to its default backend."
        ),
    )


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
    agent_backend: str | None = None

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
            agent_backend=session.agent_backend,
        )


class SessionUpdate(BaseModel):
    """Patch a session in place.

    Stage J UI shipped a Fork DAG that surfaces every branch as a node
    on the right sidebar; the title rendered on each node is the value
    auto-named at branch time and was previously immutable. This schema
    powers ``PATCH /api/sessions/{id}`` so users can rename a node after
    the fact -- handy when the auto-namer's first pass is stale or
    cryptic. Only ``title`` is exposed for now; status / backend remain
    write-once via their dedicated endpoints.
    """

    title: str = Field(..., min_length=1, max_length=200)


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
    agent_backend: str | None = Field(
        default=None,
        description=(
            "Override the parent session's backend for this fork. "
            "Defaults to inheriting the parent's backend."
        ),
    )


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


# Branch (freeze + auto-name + fork in one round trip) ----------------------


class SessionBranch(BaseModel):
    """Branch off the parent session by freezing then auto-naming a fork.

    Stage J wraps the existing freeze + fork pipeline in a single
    endpoint so the chat shell can branch the live conversation with a
    one-click affordance:

    1. Freeze the parent's turn range (defaults to the whole session).
    2. Ask :class:`AutoNamer` for a short title summarising the parent's
       recent intent.
    3. Create a child :class:`Session` whose ``parent_version_id`` /
       ``fork_seq`` point at the freshly frozen Version.

    All fields are optional: with an empty body the handler freezes the
    whole parent session, names the fork via the configured AutoNamer,
    and pins ``fork_seq = parent's last turn``. Callers that already know
    the precise turn (per-turn "branch from here") supply both
    ``start_seq`` and ``end_seq`` to override the freeze range.
    """

    title: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description=(
            "Override the auto-named title. When omitted the configured "
            "AutoNamer (Stage J) generates one from the parent's recent turns."
        ),
    )
    start_seq: int | None = Field(default=None, ge=0)
    end_seq: int | None = Field(default=None, ge=0)
    label: str | None = Field(default=None, max_length=200)
    group_id: UUID | None = None
    agent_backend: str | None = Field(
        default=None,
        description=(
            "Override the parent session's backend for the new branch. "
            "Defaults to inheriting the parent's backend."
        ),
    )


class SessionBranchResponse(BaseModel):
    """The freshly forked child session along with the parent's new Version."""

    child: SessionRead
    parent_version: VersionRead
    auto_named: bool = Field(
        ...,
        description=(
            "True when the title came from the configured AutoNamer; "
            "False when the caller supplied ``title`` or the AutoNamer "
            "fell through to the deterministic Noop fallback."
        ),
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
    group_id: UUID | None = None

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
            group_id=node.group_id,
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
