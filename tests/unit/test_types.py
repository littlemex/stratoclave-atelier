"""Tests for :mod:`stratoclave_atelier.core.types`."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from stratoclave_atelier.core.types import Group, Session, Version


def _now() -> datetime:
    return datetime.now(tz=UTC)


def test_group_is_frozen() -> None:
    g = Group(
        group_id=uuid4(),
        name="a",
        description=None,
        created_at=_now(),
        updated_at=_now(),
    )
    with pytest.raises((AttributeError, TypeError)):
        g.name = "b"  # type: ignore[misc]


def test_session_round_trip_fields() -> None:
    sid = uuid4()
    s = Session(
        session_id=sid,
        group_id=None,
        title="t",
        parent_session_id=None,
        parent_version_id=None,
        fork_seq=None,
        status="active",
        created_at=_now(),
        updated_at=_now(),
    )
    assert s.session_id == sid
    assert s.status == "active"


def test_version_invariants_kept_in_constructor() -> None:
    v = Version(
        version_id=uuid4(),
        session_id=uuid4(),
        blob_sha="0" * 64,
        blob_path="/tmp/x",
        turn_count=3,
        start_seq=2,
        end_seq=4,
        byte_size=1234,
        label=None,
        frozen_at=_now(),
    )
    # Range invariant the migration enforces; mirror the check in code so a
    # bug here surfaces in unit tests too.
    assert v.turn_count == v.end_seq - v.start_seq + 1
