"""In-memory :class:`Store` implementation.

This implementation is the test workhorse: it keeps everything in
plain dicts protected by an :class:`asyncio.Lock`, so unit tests can
exercise the same code paths as the asyncpg backend without a
database. It also serves as the executable spec for the Protocol --
when the asyncpg implementation lands, its behaviour should match
this module turn-for-turn.

Invariants enforced here (and re-checked by Postgres in the asyncpg
backend):

* ``end_seq >= start_seq`` and ``turn_count = end_seq - start_seq + 1``
  on :meth:`InMemoryStore.create_version`.
* fork sessions must reference a version whose ``session_id`` matches
  the supplied ``parent_session_id``.
* Events use a monotonic ``seq`` allocated inside the store.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from stratoclave_atelier.core import (
    ConflictError,
    Event,
    EventKind,
    Group,
    NotFoundError,
    Session,
    SessionStatus,
    SnapshotQuery,
    Version,
)
from stratoclave_atelier.db.store import Store


class InMemoryStore(Store):
    """Process-local store backed by dicts.

    Suitable for unit tests and prototypes; not safe across processes
    and not durable. Uses a single asyncio lock to serialise mutations
    so concurrent ``await`` callers see a consistent state.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._groups: dict[UUID, Group] = {}
        self._sessions: dict[UUID, Session] = {}
        self._versions: dict[UUID, Version] = {}
        self._events: dict[UUID, list[Event]] = {}
        self._next_seq: dict[UUID, int] = {}
        self._snapshot_queries: dict[UUID, SnapshotQuery] = {}

    @staticmethod
    def _now() -> datetime:
        return datetime.now(tz=UTC)

    # groups -----------------------------------------------------------------
    async def create_group(self, *, name: str, description: str | None) -> Group:
        async with self._lock:
            now = self._now()
            group = Group(
                group_id=uuid4(),
                name=name,
                description=description,
                created_at=now,
                updated_at=now,
            )
            self._groups[group.group_id] = group
            return group

    async def get_group(self, group_id: UUID) -> Group:
        async with self._lock:
            try:
                return self._groups[group_id]
            except KeyError as exc:
                raise NotFoundError(f"group {group_id} not found") from exc

    async def list_groups(self) -> list[Group]:
        async with self._lock:
            return sorted(self._groups.values(), key=lambda g: g.created_at)

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
    ) -> Session:
        async with self._lock:
            if group_id is not None and group_id not in self._groups:
                raise NotFoundError(f"group {group_id} not found")

            if parent_session_id is not None and parent_session_id not in self._sessions:
                raise NotFoundError(f"session {parent_session_id} not found")

            if parent_version_id is not None:
                parent_version = self._versions.get(parent_version_id)
                if parent_version is None:
                    raise NotFoundError(f"version {parent_version_id} not found")
                if parent_version.session_id != parent_session_id:
                    raise ConflictError("parent_version_id does not belong to parent_session_id")
                if fork_seq is None:
                    raise ConflictError("fork_seq is required when forking from a version")
                if fork_seq < parent_version.start_seq or fork_seq > parent_version.end_seq:
                    raise ConflictError("fork_seq must lie within the parent version's turn range")

            # Forks inherit the parent's backend by default so a
            # mid-conversation fork never crosses backends silently.
            if agent_backend is None and parent_session_id is not None:
                parent_session = self._sessions.get(parent_session_id)
                if parent_session is not None:
                    agent_backend = parent_session.agent_backend

            now = self._now()
            session = Session(
                session_id=uuid4(),
                group_id=group_id,
                title=title,
                parent_session_id=parent_session_id,
                parent_version_id=parent_version_id,
                fork_seq=fork_seq,
                status="active",
                created_at=now,
                updated_at=now,
                agent_backend=agent_backend,
            )
            self._sessions[session.session_id] = session
            self._events[session.session_id] = []
            self._next_seq[session.session_id] = 0
            return session

    async def get_session(self, session_id: UUID) -> Session:
        async with self._lock:
            try:
                return self._sessions[session_id]
            except KeyError as exc:
                raise NotFoundError(f"session {session_id} not found") from exc

    async def list_sessions(self, *, group_id: UUID | None = None) -> list[Session]:
        async with self._lock:
            sessions = list(self._sessions.values())
            if group_id is not None:
                sessions = [s for s in sessions if s.group_id == group_id]
            return sorted(sessions, key=lambda s: s.created_at)

    async def update_session_status(self, session_id: UUID, status: SessionStatus) -> Session:
        async with self._lock:
            current = self._sessions.get(session_id)
            if current is None:
                raise NotFoundError(f"session {session_id} not found")
            updated = Session(
                session_id=current.session_id,
                group_id=current.group_id,
                title=current.title,
                parent_session_id=current.parent_session_id,
                parent_version_id=current.parent_version_id,
                fork_seq=current.fork_seq,
                status=status,
                created_at=current.created_at,
                updated_at=self._now(),
                agent_backend=current.agent_backend,
            )
            self._sessions[session_id] = updated
            return updated

    async def update_session_title(self, session_id: UUID, title: str) -> Session:
        normalised = title.strip()
        if not normalised:
            raise ConflictError("title must not be empty")
        if len(normalised) > 200:
            raise ConflictError("title must be <= 200 characters")
        async with self._lock:
            current = self._sessions.get(session_id)
            if current is None:
                raise NotFoundError(f"session {session_id} not found")
            updated = Session(
                session_id=current.session_id,
                group_id=current.group_id,
                title=normalised,
                parent_session_id=current.parent_session_id,
                parent_version_id=current.parent_version_id,
                fork_seq=current.fork_seq,
                status=current.status,
                created_at=current.created_at,
                updated_at=self._now(),
                agent_backend=current.agent_backend,
            )
            self._sessions[session_id] = updated
            return updated

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
    ) -> Version:
        async with self._lock:
            if session_id not in self._sessions:
                raise NotFoundError(f"session {session_id} not found")
            if start_seq < 0:
                raise ConflictError("start_seq must be >= 0")
            if end_seq < start_seq:
                raise ConflictError("end_seq must be >= start_seq")
            if byte_size < 0:
                raise ConflictError("byte_size must be >= 0")

            version = Version(
                version_id=uuid4(),
                session_id=session_id,
                blob_sha=blob_sha,
                blob_path=blob_path,
                turn_count=end_seq - start_seq + 1,
                start_seq=start_seq,
                end_seq=end_seq,
                byte_size=byte_size,
                label=label,
                frozen_at=self._now(),
            )
            self._versions[version.version_id] = version
            return version

    async def get_version(self, version_id: UUID) -> Version:
        async with self._lock:
            try:
                return self._versions[version_id]
            except KeyError as exc:
                raise NotFoundError(f"version {version_id} not found") from exc

    async def list_versions(self, session_id: UUID) -> list[Version]:
        async with self._lock:
            if session_id not in self._sessions:
                raise NotFoundError(f"session {session_id} not found")
            versions = [v for v in self._versions.values() if v.session_id == session_id]
            return sorted(versions, key=lambda v: v.frozen_at, reverse=True)

    # events -----------------------------------------------------------------
    async def append_event(
        self,
        *,
        session_id: UUID,
        kind: EventKind,
        payload: dict[str, Any],
    ) -> Event:
        async with self._lock:
            if session_id not in self._sessions:
                raise NotFoundError(f"session {session_id} not found")
            seq = self._next_seq[session_id]
            event = Event(
                event_id=uuid4(),
                session_id=session_id,
                seq=seq,
                kind=kind,
                payload=payload,
                created_at=self._now(),
            )
            self._events[session_id].append(event)
            self._next_seq[session_id] = seq + 1
            return event

    async def list_events(self, session_id: UUID, *, from_seq: int = 0) -> list[Event]:
        async with self._lock:
            if session_id not in self._sessions:
                raise NotFoundError(f"session {session_id} not found")
            return [e for e in self._events[session_id] if e.seq >= from_seq]

    async def next_seq(self, session_id: UUID) -> int:
        async with self._lock:
            if session_id not in self._sessions:
                raise NotFoundError(f"session {session_id} not found")
            return self._next_seq[session_id]

    # snapshot queries -------------------------------------------------------
    async def create_snapshot_query(
        self,
        *,
        source_session_id: UUID,
        target_version_id: UUID,
        query: str,
        response: str | None = None,
    ) -> SnapshotQuery:
        async with self._lock:
            if source_session_id not in self._sessions:
                raise NotFoundError(f"session {source_session_id} not found")
            if target_version_id not in self._versions:
                raise NotFoundError(f"version {target_version_id} not found")

            row = SnapshotQuery(
                query_id=uuid4(),
                source_session_id=source_session_id,
                target_version_id=target_version_id,
                query=query,
                response=response,
                created_at=self._now(),
            )
            self._snapshot_queries[row.query_id] = row
            return row

    async def list_snapshot_queries(
        self,
        *,
        source_session_id: UUID | None = None,
        target_version_id: UUID | None = None,
    ) -> list[SnapshotQuery]:
        async with self._lock:
            rows = list(self._snapshot_queries.values())
            if source_session_id is not None:
                rows = [r for r in rows if r.source_session_id == source_session_id]
            if target_version_id is not None:
                rows = [r for r in rows if r.target_version_id == target_version_id]
            return sorted(rows, key=lambda r: r.created_at)
