"""Domain types for stratoclave-atelier.

These are the in-memory representations of the rows persisted by the
five atelier tables (``groups``, ``sessions``, ``versions``, ``events``,
``snapshot_queries``). They are deliberately plain frozen dataclasses
so that we can serialise them via Pydantic in the API layer without
coupling the domain model to a web framework.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

SessionStatus = Literal["active", "frozen", "archived"]
EventKind = Literal[
    "turn",
    "freeze",
    "fork",
    "system",
    # Stage G: agent runs persist their stream to the same event log.
    "agent_chunk",
    "agent_turn",
    "agent_error",
]


@dataclass(frozen=True, slots=True)
class Group:
    """A container for related sessions."""

    group_id: UUID
    name: str
    description: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class Session:
    """An individual agent conversation.

    ``parent_session_id`` and ``parent_version_id`` form the fork DAG.
    A session forked from another inherits turns ``[0, fork_seq]`` from
    the parent's frozen version; subsequent turns are appended only to
    this session.

    ``agent_backend`` records the loom backend chosen at session
    creation time (Stage H). ``None`` means "use the server-default
    backend"; Stage G sessions persisted before the migration are
    therefore handled transparently.
    """

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


@dataclass(frozen=True, slots=True)
class Version:
    """An immutable, content-addressed JSONL snapshot.

    ``blob_sha`` is the SHA-256 of the JSONL bytes; identical content
    collapses to one blob even if frozen multiple times. ``start_seq``
    and ``end_seq`` define the closed range of turn sequence numbers
    captured in the version (so a per-turn freeze of turn N sets
    ``start_seq = end_seq = N``).
    """

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


@dataclass(frozen=True, slots=True)
class Event:
    """A single entry in a session's monotonic event log.

    ``seq`` is unique per session and monotonically increasing. ``kind``
    distinguishes turn appends from control events (freeze / fork). The
    JSONL turn payload is stored as-is in ``payload`` so SSE replay can
    re-emit it without parsing.
    """

    event_id: UUID
    session_id: UUID
    seq: int
    kind: EventKind
    payload: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SnapshotQuery:
    """Audit row for a cross-session question against a frozen Version.

    Every Stage D ``snapshot-query`` request is recorded synchronously so
    operators can later answer "which sessions referenced this frozen
    version?". ``response`` carries the resolver's answer (an LLM digest
    in production, an echo of the query in the default in-process
    resolver used for tests and the stand-alone walking skeleton).
    """

    query_id: UUID
    source_session_id: UUID
    target_version_id: UUID
    query: str
    response: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ForkGraphVersion:
    """A version reference embedded inside :class:`ForkGraphNode`."""

    version_id: UUID
    label: str | None
    start_seq: int
    end_seq: int
    turn_count: int


@dataclass(frozen=True, slots=True)
class ForkGraphNode:
    """One node in the fork DAG visualised by Stage E.

    ``frozen`` flags whether the node corresponds to a frozen version
    (we render those with a different stroke). ``parent_version_id`` is
    the version this session was forked from (``None`` for root
    sessions), and ``fork_seq`` is the turn at which the fork was taken.
    """

    session_id: UUID
    title: str
    status: SessionStatus
    parent_session_id: UUID | None
    parent_version_id: UUID | None
    fork_seq: int | None
    versions: tuple[ForkGraphVersion, ...]


@dataclass(frozen=True, slots=True)
class ForkGraphEdge:
    """A parent->child edge in the fork DAG.

    ``via_version_id`` is the version that the child was forked from;
    the edge therefore carries enough information to render
    "forked from <version label> at turn N".
    """

    parent_session_id: UUID
    child_session_id: UUID
    via_version_id: UUID
    fork_seq: int
