"""Pure helpers for building the fork DAG visualised by Stage E.

The atelier UI renders a directed acyclic graph where each node is a
session and each edge "session A forked to session B at version V, turn
N". The shape of the graph is fully derivable from the ``sessions`` and
``versions`` tables, so this module is intentionally a pure function:
the API layer pulls rows from the :class:`Store`, hands them to
:func:`build_fork_graph`, and serialises the result via
:mod:`stratoclave_atelier.api.schemas`.

Keeping the topology logic outside the store also means we can unit-test
it with synthetic dataclasses, without spinning up Postgres.

The returned tuple is ``(nodes, edges)`` where:

* ``nodes`` -- one :class:`ForkGraphNode` per supplied session, with the
  session's own versions attached (sorted by ``end_seq``). Sessions with
  no versions get an empty tuple, which is what the SVG renderer
  expects.
* ``edges`` -- one :class:`ForkGraphEdge` per session that has a
  ``parent_version_id`` (i.e. was forked). The edge carries the parent
  session id, the child session id, the via-version, and the
  ``fork_seq`` so the UI can label "forked from <label> at turn N".

Sessions whose ``parent_version_id`` references a version not in the
supplied ``versions`` list are still emitted as nodes, but no edge is
generated for them: this mirrors how the UI handles fork-graphs for a
single group (versions outside the group's sessions are filtered out
upstream).
"""

from __future__ import annotations

from collections.abc import Iterable

from stratoclave_atelier.core import (
    ForkGraphEdge,
    ForkGraphNode,
    ForkGraphVersion,
    Session,
    Version,
)


def build_fork_graph(
    sessions: Iterable[Session],
    versions: Iterable[Version],
) -> tuple[list[ForkGraphNode], list[ForkGraphEdge]]:
    """Materialise the fork DAG from sessions + versions.

    The function is deterministic: nodes are returned in the same order
    as ``sessions``; edges are returned in the order of the child
    sessions. Versions inside a node are sorted by ``end_seq`` so the UI
    can render them as a left-to-right timeline.
    """

    sessions_list = list(sessions)
    versions_list = list(versions)

    versions_by_session: dict[Session, list[Version]] = {}
    for s in sessions_list:
        versions_by_session[s] = []
    for v in versions_list:
        for s in sessions_list:
            if s.session_id == v.session_id:
                versions_by_session[s].append(v)
                break

    versions_by_id = {v.version_id: v for v in versions_list}

    nodes: list[ForkGraphNode] = []
    for s in sessions_list:
        own_versions = sorted(versions_by_session[s], key=lambda v: v.end_seq)
        nodes.append(
            ForkGraphNode(
                session_id=s.session_id,
                title=s.title,
                status=s.status,
                parent_session_id=s.parent_session_id,
                parent_version_id=s.parent_version_id,
                fork_seq=s.fork_seq,
                versions=tuple(
                    ForkGraphVersion(
                        version_id=v.version_id,
                        label=v.label,
                        start_seq=v.start_seq,
                        end_seq=v.end_seq,
                        turn_count=v.turn_count,
                    )
                    for v in own_versions
                ),
            )
        )

    edges: list[ForkGraphEdge] = []
    for s in sessions_list:
        if s.parent_version_id is None or s.parent_session_id is None or s.fork_seq is None:
            continue
        parent_version = versions_by_id.get(s.parent_version_id)
        if parent_version is None:
            # Parent version is outside the supplied window (e.g. group
            # filter); skip the edge but keep the child node.
            continue
        edges.append(
            ForkGraphEdge(
                parent_session_id=s.parent_session_id,
                child_session_id=s.session_id,
                via_version_id=s.parent_version_id,
                fork_seq=s.fork_seq,
            )
        )

    return nodes, edges
