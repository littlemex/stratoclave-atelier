"""asyncpg-backed :class:`Store` implementation.

Uses SQLAlchemy's async engine to manage connections (so we share the
same connection lifecycle conventions as alembic) but issues plain SQL
through ``connection.execute()`` -- this keeps the implementation
straightforward and the SQL grep-able when debugging.

The schema is defined in :mod:`migrations.versions.0001_initial_schema`;
this module only reads/writes existing rows. Schema mismatches surface
as ``asyncpg`` errors rather than custom :class:`SchemaError`s; that
remains a follow-up.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

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


def create_engine(database_url: str) -> AsyncEngine:
    """Build an async engine; pool defaults are SQLAlchemy's own.

    Kept as a tiny helper so app lifespan code reads cleanly:
    ``engine = create_engine(config.database_url)``.
    """

    return create_async_engine(database_url, future=True)


class AsyncpgStore(Store):
    """Postgres-backed store using SQLAlchemy's async engine."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    async def dispose(self) -> None:
        await self._engine.dispose()

    # groups -----------------------------------------------------------------
    _GROUP_COLUMNS = "group_id, name, description, color, created_at, updated_at"

    async def create_group(
        self,
        *,
        name: str,
        description: str | None,
        color: str,
    ) -> Group:
        group_id = uuid4()
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(
                    text(
                        "INSERT INTO groups (group_id, name, description, color) "
                        "VALUES (:gid, :name, :desc, :color) "
                        f"RETURNING {self._GROUP_COLUMNS}"
                    ),
                    {"gid": group_id, "name": name, "desc": description, "color": color},
                )
            ).one()
        return _row_to_group(row)

    async def get_group(self, group_id: UUID) -> Group:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        f"SELECT {self._GROUP_COLUMNS} FROM groups WHERE group_id = :gid"
                    ),
                    {"gid": group_id},
                )
            ).one_or_none()
        if row is None:
            raise NotFoundError(f"group {group_id} not found")
        return _row_to_group(row)

    async def list_groups(self) -> list[Group]:
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        f"SELECT {self._GROUP_COLUMNS} FROM groups ORDER BY created_at"
                    )
                )
            ).all()
        return [_row_to_group(r) for r in rows]

    async def update_group(
        self,
        group_id: UUID,
        *,
        name: str | None = None,
        description: str | None = None,
        color: str | None = None,
    ) -> Group:
        if name is None and description is None and color is None:
            raise ConflictError("update_group requires at least one field to change")
        sets: list[str] = []
        params: dict[str, Any] = {"gid": group_id}
        if name is not None:
            sets.append("name = :name")
            params["name"] = name
        if description is not None:
            sets.append("description = :desc")
            params["desc"] = description
        if color is not None:
            sets.append("color = :color")
            params["color"] = color
        sets.append("updated_at = now()")
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(
                    text(
                        f"UPDATE groups SET {', '.join(sets)} "
                        "WHERE group_id = :gid "
                        f"RETURNING {self._GROUP_COLUMNS}"
                    ),
                    params,
                )
            ).one_or_none()
        if row is None:
            raise NotFoundError(f"group {group_id} not found")
        return _row_to_group(row)

    async def delete_group(self, group_id: UUID) -> None:
        async with self._engine.begin() as conn:
            result = await conn.execute(
                text("DELETE FROM groups WHERE group_id = :gid"),
                {"gid": group_id},
            )
        if result.rowcount == 0:
            raise NotFoundError(f"group {group_id} not found")

    async def update_session_group(
        self,
        session_id: UUID,
        group_id: UUID | None,
    ) -> Session:
        async with self._engine.begin() as conn:
            current = (
                await conn.execute(
                    text(
                        f"SELECT {self._SESSION_COLUMNS} FROM sessions "
                        "WHERE session_id = :sid FOR UPDATE"
                    ),
                    {"sid": session_id},
                )
            ).one_or_none()
            if current is None:
                raise NotFoundError(f"session {session_id} not found")
            if current.parent_session_id is not None:
                raise ConflictError(
                    "only root sessions can be assigned to a group; "
                    f"session {session_id} is a fork of {current.parent_session_id}"
                )
            try:
                row = (
                    await conn.execute(
                        text(
                            "UPDATE sessions "
                            "SET group_id = :gid, updated_at = now() "
                            "WHERE session_id = :sid "
                            f"RETURNING {self._SESSION_COLUMNS}"
                        ),
                        {"sid": session_id, "gid": group_id},
                    )
                ).one()
            except IntegrityError as exc:
                raise NotFoundError(str(exc.orig)) from exc
        return _row_to_session(row)

    # sessions ---------------------------------------------------------------
    _SESSION_COLUMNS = (
        "session_id, group_id, title,"
        " parent_session_id, parent_version_id, fork_seq,"
        " status, created_at, updated_at, agent_backend"
    )

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
        await self._validate_fork(parent_session_id, parent_version_id, fork_seq)

        # Forks inherit the parent's backend by default to keep
        # mid-conversation forks on the same engine.
        if agent_backend is None and parent_session_id is not None:
            parent = await self.get_session(parent_session_id)
            agent_backend = parent.agent_backend

        session_id = uuid4()
        async with self._engine.begin() as conn:
            try:
                row = (
                    await conn.execute(
                        text(
                            "INSERT INTO sessions ("
                            " session_id, group_id, title,"
                            " parent_session_id, parent_version_id, fork_seq,"
                            " agent_backend"
                            ") VALUES (:sid, :gid, :title, :psid, :pvid, :fseq, :ab) "
                            f"RETURNING {self._SESSION_COLUMNS}"
                        ),
                        {
                            "sid": session_id,
                            "gid": group_id,
                            "title": title,
                            "psid": parent_session_id,
                            "pvid": parent_version_id,
                            "fseq": fork_seq,
                            "ab": agent_backend,
                        },
                    )
                ).one()
            except IntegrityError as exc:
                # Most likely a missing FK target.
                raise NotFoundError(str(exc.orig)) from exc
        return _row_to_session(row)

    async def get_session(self, session_id: UUID) -> Session:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    text(f"SELECT {self._SESSION_COLUMNS} FROM sessions WHERE session_id = :sid"),
                    {"sid": session_id},
                )
            ).one_or_none()
        if row is None:
            raise NotFoundError(f"session {session_id} not found")
        return _row_to_session(row)

    async def list_sessions(self, *, group_id: UUID | None = None) -> list[Session]:
        sql = f"SELECT {self._SESSION_COLUMNS} FROM sessions"
        params: dict[str, Any] = {}
        if group_id is not None:
            sql += " WHERE group_id = :gid"
            params["gid"] = group_id
        sql += " ORDER BY created_at"
        async with self._engine.connect() as conn:
            rows = (await conn.execute(text(sql), params)).all()
        return [_row_to_session(r) for r in rows]

    async def update_session_status(self, session_id: UUID, status: SessionStatus) -> Session:
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(
                    text(
                        "UPDATE sessions SET status = :status, updated_at = now() "
                        "WHERE session_id = :sid "
                        f"RETURNING {self._SESSION_COLUMNS}"
                    ),
                    {"sid": session_id, "status": status},
                )
            ).one_or_none()
        if row is None:
            raise NotFoundError(f"session {session_id} not found")
        return _row_to_session(row)

    async def update_session_title(self, session_id: UUID, title: str) -> Session:
        normalised = title.strip()
        if not normalised:
            raise ConflictError("title must not be empty")
        if len(normalised) > 200:
            raise ConflictError("title must be <= 200 characters")
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(
                    text(
                        "UPDATE sessions SET title = :title, updated_at = now() "
                        "WHERE session_id = :sid "
                        f"RETURNING {self._SESSION_COLUMNS}"
                    ),
                    {"sid": session_id, "title": normalised},
                )
            ).one_or_none()
        if row is None:
            raise NotFoundError(f"session {session_id} not found")
        return _row_to_session(row)

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
        if start_seq < 0:
            raise ConflictError("start_seq must be >= 0")
        if end_seq < start_seq:
            raise ConflictError("end_seq must be >= start_seq")
        if byte_size < 0:
            raise ConflictError("byte_size must be >= 0")

        version_id = uuid4()
        turn_count = end_seq - start_seq + 1
        async with self._engine.begin() as conn:
            try:
                row = (
                    await conn.execute(
                        text(
                            "INSERT INTO versions ("
                            " version_id, session_id, blob_sha, blob_path,"
                            " turn_count, start_seq, end_seq, byte_size, label"
                            ") VALUES (:vid, :sid, :sha, :path,"
                            " :tc, :ss, :es, :bs, :label) "
                            "RETURNING version_id, session_id, blob_sha, blob_path,"
                            " turn_count, start_seq, end_seq, byte_size, label, frozen_at"
                        ),
                        {
                            "vid": version_id,
                            "sid": session_id,
                            "sha": blob_sha,
                            "path": blob_path,
                            "tc": turn_count,
                            "ss": start_seq,
                            "es": end_seq,
                            "bs": byte_size,
                            "label": label,
                        },
                    )
                ).one()
            except IntegrityError as exc:
                raise NotFoundError(str(exc.orig)) from exc
        return _row_to_version(row)

    async def get_version(self, version_id: UUID) -> Version:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT version_id, session_id, blob_sha, blob_path,"
                        " turn_count, start_seq, end_seq, byte_size, label, frozen_at "
                        "FROM versions WHERE version_id = :vid"
                    ),
                    {"vid": version_id},
                )
            ).one_or_none()
        if row is None:
            raise NotFoundError(f"version {version_id} not found")
        return _row_to_version(row)

    async def list_versions(self, session_id: UUID) -> list[Version]:
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT version_id, session_id, blob_sha, blob_path,"
                        " turn_count, start_seq, end_seq, byte_size, label, frozen_at "
                        "FROM versions WHERE session_id = :sid "
                        "ORDER BY frozen_at DESC"
                    ),
                    {"sid": session_id},
                )
            ).all()
        if not rows:
            # Distinguish "session has no versions" from "session does not exist".
            await self.get_session(session_id)
        return [_row_to_version(r) for r in rows]

    # events -----------------------------------------------------------------
    async def append_event(
        self,
        *,
        session_id: UUID,
        kind: EventKind,
        payload: dict[str, Any],
    ) -> Event:
        event_id = uuid4()
        async with self._engine.begin() as conn:
            # Ensure parent session exists (and lock it implicitly via the
            # SELECT FOR UPDATE pattern).
            existing = (
                await conn.execute(
                    text("SELECT 1 FROM sessions WHERE session_id = :sid FOR UPDATE"),
                    {"sid": session_id},
                )
            ).one_or_none()
            if existing is None:
                raise NotFoundError(f"session {session_id} not found")

            seq = (
                await conn.execute(
                    text(
                        "SELECT COALESCE(MAX(seq) + 1, 0) AS next "
                        "FROM events WHERE session_id = :sid"
                    ),
                    {"sid": session_id},
                )
            ).scalar_one()

            row = (
                await conn.execute(
                    text(
                        "INSERT INTO events ("
                        " event_id, session_id, seq, kind, payload"
                        ") VALUES (:eid, :sid, :seq, :kind, CAST(:payload AS jsonb)) "
                        "RETURNING event_id, session_id, seq, kind, payload, created_at"
                    ),
                    {
                        "eid": event_id,
                        "sid": session_id,
                        "seq": int(seq),
                        "kind": kind,
                        "payload": json.dumps(payload),
                    },
                )
            ).one()
        return _row_to_event(row)

    async def list_events(self, session_id: UUID, *, from_seq: int = 0) -> list[Event]:
        async with self._engine.connect() as conn:
            existing = (
                await conn.execute(
                    text("SELECT 1 FROM sessions WHERE session_id = :sid"),
                    {"sid": session_id},
                )
            ).one_or_none()
            if existing is None:
                raise NotFoundError(f"session {session_id} not found")
            rows = (
                await conn.execute(
                    text(
                        "SELECT event_id, session_id, seq, kind, payload, created_at "
                        "FROM events WHERE session_id = :sid AND seq >= :from_seq "
                        "ORDER BY seq"
                    ),
                    {"sid": session_id, "from_seq": from_seq},
                )
            ).all()
        return [_row_to_event(r) for r in rows]

    async def next_seq(self, session_id: UUID) -> int:
        async with self._engine.connect() as conn:
            existing = (
                await conn.execute(
                    text("SELECT 1 FROM sessions WHERE session_id = :sid"),
                    {"sid": session_id},
                )
            ).one_or_none()
            if existing is None:
                raise NotFoundError(f"session {session_id} not found")
            value = (
                await conn.execute(
                    text("SELECT COALESCE(MAX(seq) + 1, 0) FROM events WHERE session_id = :sid"),
                    {"sid": session_id},
                )
            ).scalar_one()
        return int(value)

    # snapshot queries -------------------------------------------------------
    async def create_snapshot_query(
        self,
        *,
        source_session_id: UUID,
        target_version_id: UUID,
        query: str,
        response: str | None = None,
    ) -> SnapshotQuery:
        # Validate both parents exist; surface NotFoundError for either.
        await self.get_session(source_session_id)
        await self.get_version(target_version_id)

        query_id = uuid4()
        async with self._engine.begin() as conn:
            try:
                row = (
                    await conn.execute(
                        text(
                            "INSERT INTO snapshot_queries ("
                            " query_id, source_session_id, target_version_id, query, response"
                            ") VALUES (:qid, :ssid, :tvid, :q, :r) "
                            "RETURNING query_id, source_session_id, target_version_id,"
                            " query, response, created_at"
                        ),
                        {
                            "qid": query_id,
                            "ssid": source_session_id,
                            "tvid": target_version_id,
                            "q": query,
                            "r": response,
                        },
                    )
                ).one()
            except IntegrityError as exc:
                raise NotFoundError(str(exc.orig)) from exc
        return _row_to_snapshot_query(row)

    async def list_snapshot_queries(
        self,
        *,
        source_session_id: UUID | None = None,
        target_version_id: UUID | None = None,
    ) -> list[SnapshotQuery]:
        sql = (
            "SELECT query_id, source_session_id, target_version_id,"
            " query, response, created_at "
            "FROM snapshot_queries"
        )
        params: dict[str, Any] = {}
        clauses: list[str] = []
        if source_session_id is not None:
            clauses.append("source_session_id = :ssid")
            params["ssid"] = source_session_id
        if target_version_id is not None:
            clauses.append("target_version_id = :tvid")
            params["tvid"] = target_version_id
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at"
        async with self._engine.connect() as conn:
            rows = (await conn.execute(text(sql), params)).all()
        return [_row_to_snapshot_query(r) for r in rows]

    # internals --------------------------------------------------------------
    async def _validate_fork(
        self,
        parent_session_id: UUID | None,
        parent_version_id: UUID | None,
        fork_seq: int | None,
    ) -> None:
        if parent_version_id is None:
            return
        version = await self.get_version(parent_version_id)
        if version.session_id != parent_session_id:
            raise ConflictError("parent_version_id does not belong to parent_session_id")
        if fork_seq is None:
            raise ConflictError("fork_seq is required when forking from a version")
        if fork_seq < version.start_seq or fork_seq > version.end_seq:
            raise ConflictError("fork_seq must lie within the parent version's turn range")


# Row mappers -----------------------------------------------------------------


def _row_to_group(row: Any) -> Group:
    return Group(
        group_id=row.group_id,
        name=row.name,
        description=row.description,
        color=row.color,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _row_to_session(row: Any) -> Session:
    return Session(
        session_id=row.session_id,
        group_id=row.group_id,
        title=row.title,
        parent_session_id=row.parent_session_id,
        parent_version_id=row.parent_version_id,
        fork_seq=row.fork_seq,
        status=row.status,
        created_at=row.created_at,
        updated_at=row.updated_at,
        agent_backend=getattr(row, "agent_backend", None),
    )


def _row_to_version(row: Any) -> Version:
    return Version(
        version_id=row.version_id,
        session_id=row.session_id,
        blob_sha=row.blob_sha,
        blob_path=row.blob_path,
        turn_count=row.turn_count,
        start_seq=row.start_seq,
        end_seq=row.end_seq,
        byte_size=row.byte_size,
        label=row.label,
        frozen_at=row.frozen_at,
    )


def _row_to_event(row: Any) -> Event:
    payload = row.payload
    if isinstance(payload, str):
        payload = json.loads(payload)
    return Event(
        event_id=row.event_id,
        session_id=row.session_id,
        seq=row.seq,
        kind=row.kind,
        payload=payload,
        created_at=row.created_at,
    )


def _row_to_snapshot_query(row: Any) -> SnapshotQuery:
    return SnapshotQuery(
        query_id=row.query_id,
        source_session_id=row.source_session_id,
        target_version_id=row.target_version_id,
        query=row.query,
        response=row.response,
        created_at=row.created_at,
    )
