"""Store abstractions for stratoclave-atelier.

The :class:`Store` Protocol is the read/write surface that the API layer
talks to. Stage B ships two implementations: :class:`InMemoryStore` for
unit tests and the asyncpg-backed implementation in
:mod:`stratoclave_atelier.db.asyncpg_store` for the runtime. Keeping
both behind a single Protocol means the same REST handlers and the same
test suites can exercise either backend.

Design notes
------------
* All write methods raise :class:`NotFoundError` when a referenced
  parent (group / session / version) does not exist, and
  :class:`ConflictError` when a write would violate a domain invariant
  (e.g. fork referencing a version that belongs to a different session).
* Events use ``next_seq`` to allocate the next monotonic ``seq`` for a
  session inside the store; callers should not invent ``seq`` values.
* All methods are ``async`` so the same Protocol fits both the
  in-memory implementation (which awaits trivially) and the asyncpg
  implementation.
"""

from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID

from stratoclave_atelier.core import (
    Event,
    EventKind,
    Group,
    Session,
    SessionStatus,
    SnapshotQuery,
    Version,
)


class Store(Protocol):
    """Read/write surface for the five atelier tables."""

    # groups -----------------------------------------------------------------
    async def create_group(
        self,
        *,
        name: str,
        description: str | None,
        color: str,
    ) -> Group: ...

    async def get_group(self, group_id: UUID) -> Group: ...

    async def list_groups(self) -> list[Group]: ...

    async def update_group(
        self,
        group_id: UUID,
        *,
        name: str | None = None,
        description: str | None = None,
        color: str | None = None,
    ) -> Group:
        """Patch a group in place.

        ``None`` means "no change" for each field; the handler rejects
        calls with every field ``None`` so an empty PATCH body never
        silently no-ops.
        """
        ...

    async def delete_group(self, group_id: UUID) -> None: ...

    async def update_session_group(
        self,
        session_id: UUID,
        group_id: UUID | None,
    ) -> Session:
        """Move ``session_id`` into ``group_id`` (or detach when ``None``).

        Raises :class:`ConflictError` when the session is not a root
        (``parent_session_id IS NOT NULL``) -- forks inherit grouping
        transitively via their parent and should not be re-grouped
        independently.
        """
        ...

    # sessions ---------------------------------------------------------------
    async def create_session(
        self,
        *,
        title: str,
        group_id: UUID | None = None,
        parent_session_id: UUID | None = None,
        parent_version_id: UUID | None = None,
        fork_seq: int | None = None,
        agent_backend: str | None = None,
    ) -> Session: ...

    async def get_session(self, session_id: UUID) -> Session: ...

    async def list_sessions(self, *, group_id: UUID | None = None) -> list[Session]: ...

    async def update_session_status(self, session_id: UUID, status: SessionStatus) -> Session: ...

    async def update_session_title(self, session_id: UUID, title: str) -> Session: ...

    # versions ---------------------------------------------------------------
    async def create_version(
        self,
        *,
        session_id: UUID,
        blob_sha: str,
        blob_path: str,
        start_seq: int,
        end_seq: int,
        byte_size: int,
        label: str | None = None,
    ) -> Version: ...

    async def get_version(self, version_id: UUID) -> Version: ...

    async def list_versions(self, session_id: UUID) -> list[Version]: ...

    # events -----------------------------------------------------------------
    async def append_event(
        self,
        *,
        session_id: UUID,
        kind: EventKind,
        payload: dict[str, Any],
    ) -> Event: ...

    async def list_events(self, session_id: UUID, *, from_seq: int = 0) -> list[Event]: ...

    async def next_seq(self, session_id: UUID) -> int: ...

    # snapshot queries -------------------------------------------------------
    async def create_snapshot_query(
        self,
        *,
        source_session_id: UUID,
        target_version_id: UUID,
        query: str,
        response: str | None = None,
    ) -> SnapshotQuery: ...

    async def list_snapshot_queries(
        self,
        *,
        source_session_id: UUID | None = None,
        target_version_id: UUID | None = None,
    ) -> list[SnapshotQuery]: ...
