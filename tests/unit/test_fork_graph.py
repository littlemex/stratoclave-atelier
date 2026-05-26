"""Unit tests for the pure :mod:`fork_graph` helper."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from stratoclave_atelier.core import Session, Version
from stratoclave_atelier.fork_graph import build_fork_graph


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _session(
    *,
    title: str,
    parent_session_id: UUID | None = None,
    parent_version_id: UUID | None = None,
    fork_seq: int | None = None,
) -> Session:
    return Session(
        session_id=uuid4(),
        group_id=None,
        title=title,
        parent_session_id=parent_session_id,
        parent_version_id=parent_version_id,
        fork_seq=fork_seq,
        status="active",
        created_at=_now(),
        updated_at=_now(),
    )


def _version(
    *, session_id: UUID, start_seq: int, end_seq: int, label: str | None = None
) -> Version:
    return Version(
        version_id=uuid4(),
        session_id=session_id,
        blob_sha="0" * 64,
        blob_path="x.jsonl",
        turn_count=end_seq - start_seq + 1,
        start_seq=start_seq,
        end_seq=end_seq,
        byte_size=10,
        label=label,
        frozen_at=_now(),
    )


def test_root_only_session_emits_one_node_no_edges() -> None:
    s = _session(title="root")
    nodes, edges = build_fork_graph([s], [])
    assert len(nodes) == 1
    assert nodes[0].session_id == s.session_id
    assert nodes[0].versions == ()
    assert edges == []


def test_versions_attached_to_parent_node_sorted_by_end_seq() -> None:
    s = _session(title="root")
    v_late = _version(session_id=s.session_id, start_seq=3, end_seq=5, label="late")
    v_early = _version(session_id=s.session_id, start_seq=0, end_seq=2, label="early")
    nodes, edges = build_fork_graph([s], [v_late, v_early])
    assert len(nodes) == 1
    labels = [v.label for v in nodes[0].versions]
    assert labels == ["early", "late"]
    assert edges == []


def test_fork_emits_edge_with_fork_seq_and_via_version() -> None:
    parent = _session(title="parent")
    parent_v = _version(session_id=parent.session_id, start_seq=0, end_seq=5)
    child = _session(
        title="child",
        parent_session_id=parent.session_id,
        parent_version_id=parent_v.version_id,
        fork_seq=3,
    )
    nodes, edges = build_fork_graph([parent, child], [parent_v])
    assert len(nodes) == 2
    assert len(edges) == 1
    edge = edges[0]
    assert edge.parent_session_id == parent.session_id
    assert edge.child_session_id == child.session_id
    assert edge.via_version_id == parent_v.version_id
    assert edge.fork_seq == 3


def test_fork_referencing_outside_version_is_dropped_to_node_only() -> None:
    parent = _session(title="parent")
    # version exists but is not in the supplied versions list (e.g. group filter)
    missing_version_id = uuid4()
    child = _session(
        title="child",
        parent_session_id=parent.session_id,
        parent_version_id=missing_version_id,
        fork_seq=1,
    )
    nodes, edges = build_fork_graph([parent, child], [])
    assert len(nodes) == 2
    assert edges == []
