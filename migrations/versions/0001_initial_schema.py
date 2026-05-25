"""initial schema: groups, sessions, versions, events, snapshot_queries

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-05-25 00:00:00

The five tables that make up atelier v0.1:

* ``groups``           -- containers for related sessions.
* ``sessions``         -- individual agent conversations. ``parent_version_id``
                          and ``fork_seq`` form the fork DAG: a session forked
                          from another inherits turns 0..fork_seq from the
                          parent's frozen version.
* ``versions``         -- immutable, content-addressed JSONL snapshots of a
                          session (or a turn range within a session). ``blob_sha``
                          is the SHA-256 of the JSONL bytes; the same content
                          collapses to one blob even if frozen multiple times.
* ``events``           -- per-session monotonic event log used to drive the
                          SSE / WebSocket history endpoints.
* ``snapshot_queries`` -- audit log of cross-session RPC snapshot lookups,
                          so we can answer "which sessions referenced this
                          frozen version?".
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE groups (
            group_id    UUID PRIMARY KEY,
            name        TEXT NOT NULL,
            description TEXT,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX idx_groups_name ON groups(name)")

    op.execute(
        """
        CREATE TABLE sessions (
            session_id        UUID PRIMARY KEY,
            group_id          UUID REFERENCES groups(group_id) ON DELETE SET NULL,
            title             TEXT NOT NULL,
            parent_session_id UUID REFERENCES sessions(session_id) ON DELETE SET NULL,
            parent_version_id UUID,
            fork_seq          INTEGER,
            status            TEXT NOT NULL DEFAULT 'active'
                              CHECK (status IN ('active','frozen','archived')),
            created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX idx_sessions_group ON sessions(group_id)")
    op.execute("CREATE INDEX idx_sessions_parent ON sessions(parent_session_id)")
    op.execute("CREATE INDEX idx_sessions_status ON sessions(status)")

    op.execute(
        """
        CREATE TABLE versions (
            version_id  UUID PRIMARY KEY,
            session_id  UUID NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
            blob_sha    TEXT NOT NULL,
            blob_path   TEXT NOT NULL,
            turn_count  INTEGER NOT NULL,
            start_seq   INTEGER NOT NULL DEFAULT 0,
            end_seq     INTEGER NOT NULL,
            byte_size   BIGINT NOT NULL,
            label       TEXT,
            frozen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            CHECK (start_seq >= 0),
            CHECK (end_seq >= start_seq),
            CHECK (turn_count = end_seq - start_seq + 1)
        )
        """
    )
    op.execute("CREATE INDEX idx_versions_session ON versions(session_id, frozen_at DESC)")
    op.execute("CREATE INDEX idx_versions_blob_sha ON versions(blob_sha)")
    op.execute(
        "ALTER TABLE sessions "
        "ADD CONSTRAINT sessions_parent_version_fk "
        "FOREIGN KEY (parent_version_id) REFERENCES versions(version_id) ON DELETE SET NULL"
    )

    op.execute(
        """
        CREATE TABLE events (
            event_id   UUID PRIMARY KEY,
            session_id UUID NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
            seq        BIGINT NOT NULL,
            kind       TEXT NOT NULL,
            payload    JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (session_id, seq)
        )
        """
    )
    op.execute("CREATE INDEX idx_events_session_seq ON events(session_id, seq)")
    op.execute("CREATE INDEX idx_events_kind ON events(kind)")

    op.execute(
        """
        CREATE TABLE snapshot_queries (
            query_id          UUID PRIMARY KEY,
            source_session_id UUID NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
            target_version_id UUID NOT NULL REFERENCES versions(version_id) ON DELETE CASCADE,
            query             TEXT NOT NULL,
            response          TEXT,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX idx_snapshot_queries_source ON snapshot_queries(source_session_id)")
    op.execute("CREATE INDEX idx_snapshot_queries_target ON snapshot_queries(target_version_id)")


def downgrade() -> None:
    # Drop in reverse FK order.
    op.execute("ALTER TABLE sessions DROP CONSTRAINT IF EXISTS sessions_parent_version_fk")
    for table in (
        "snapshot_queries",
        "events",
        "versions",
        "sessions",
        "groups",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
