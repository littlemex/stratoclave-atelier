"""groups.color column for Stage L

Revision ID: 0003_groups_color
Revises: 0002_session_agent_backend
Create Date: 2026-05-28 00:00:00

Stage L surfaces groups in the Fork DAG pane (each root session in a
group is rendered with the group's colour). The colour is stored
alongside the group itself so the UI can render new sessions joining
the group without a second round-trip.

We store the colour as a 7-character hex string (``#RRGGBB``); the API
layer normalises and validates the format before insertion. Existing
groups get a deterministic seed colour derived from their ``group_id``
hash so rows persisted before the migration still render distinctly.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003_groups_color"
down_revision: str | None = "0002_session_agent_backend"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE groups ADD COLUMN color TEXT")
    # Backfill: pick a stable colour from a small palette using the
    # group_id's first hex digit. Avoids NULLs in the column for any row
    # written before this migration ran.
    op.execute(
        """
        UPDATE groups
        SET color = CASE substr(group_id::text, 1, 1)
            WHEN '0' THEN '#3B82F6'
            WHEN '1' THEN '#10B981'
            WHEN '2' THEN '#F59E0B'
            WHEN '3' THEN '#EF4444'
            WHEN '4' THEN '#8B5CF6'
            WHEN '5' THEN '#EC4899'
            WHEN '6' THEN '#14B8A6'
            WHEN '7' THEN '#F97316'
            WHEN '8' THEN '#6366F1'
            WHEN '9' THEN '#84CC16'
            WHEN 'a' THEN '#06B6D4'
            WHEN 'b' THEN '#A855F7'
            WHEN 'c' THEN '#22C55E'
            WHEN 'd' THEN '#EAB308'
            WHEN 'e' THEN '#F43F5E'
            ELSE '#64748B'
        END
        WHERE color IS NULL
        """
    )
    op.execute("ALTER TABLE groups ALTER COLUMN color SET NOT NULL")
    op.execute(
        "ALTER TABLE groups "
        "ADD CONSTRAINT groups_color_format CHECK (color ~ '^#[0-9A-Fa-f]{6}$')"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE groups DROP CONSTRAINT IF EXISTS groups_color_format")
    op.execute("ALTER TABLE groups DROP COLUMN IF EXISTS color")
