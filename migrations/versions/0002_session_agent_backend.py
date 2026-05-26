"""sessions.agent_backend column for Stage H

Revision ID: 0002_session_agent_backend
Revises: 0001_initial_schema
Create Date: 2026-05-27 00:00:00

Stage H lets the operator pick a loom backend per session at session
creation time (claude_code / kiro_code / mock). The chosen backend is
persisted alongside the session so reloading the chat surface --or
forking from a frozen version-- continues against the same backend.

``NULL`` means "use the default backend the server is configured with",
which keeps Stage G sessions working unchanged after the migration.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002_session_agent_backend"
down_revision: str | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE sessions "
        "ADD COLUMN agent_backend TEXT "
        "CHECK (agent_backend IN ('claude_code','kiro_code','mock'))"
    )
    op.execute("CREATE INDEX idx_sessions_agent_backend ON sessions(agent_backend)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_sessions_agent_backend")
    op.execute("ALTER TABLE sessions DROP COLUMN IF EXISTS agent_backend")
