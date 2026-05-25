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
from typing import Literal
from uuid import UUID

SessionStatus = Literal["active", "frozen", "archived"]


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
