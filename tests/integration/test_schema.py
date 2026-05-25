"""Integration test: alembic migrations apply cleanly against pgvector pg16.

Gated by the ``integration`` marker and the ``ATELIER_TEST_DATABASE_URL``
env var. CI sets both; local runs are a no-op unless the operator has
``docker compose up -d`` and exports the URL.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.integration


@pytest.mark.skipif(
    "ATELIER_TEST_DATABASE_URL" not in os.environ,
    reason="ATELIER_TEST_DATABASE_URL not set",
)
def test_five_atelier_tables_exist() -> None:
    """After alembic upgrade head, every table is present and queryable."""

    psycopg = pytest.importorskip("psycopg")
    url = os.environ["ATELIER_TEST_DATABASE_URL"]
    # Drivers in the URL distinguish asyncpg from psycopg; alembic uses the
    # psycopg variant. Strip the dialect suffix for the raw driver call.
    if "+psycopg" in url:
        dsn = url.replace("postgresql+psycopg://", "postgresql://")
    elif "+asyncpg" in url:
        dsn = url.replace("postgresql+asyncpg://", "postgresql://")
    else:
        dsn = url

    expected = {
        "groups",
        "sessions",
        "versions",
        "events",
        "snapshot_queries",
    }
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        )
        present = {row[0] for row in cur.fetchall()}
    missing = expected - present
    assert not missing, f"missing tables after migration: {missing}"
