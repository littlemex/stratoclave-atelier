"""``/api/groups/{id}/fork-graph`` and ``/api/sessions/{id}/fork-graph``.

Stage D exposes the fork DAG so the SPA can render an SVG of "session
A forked to session B at version V, turn N". Two scopes are supported:

* per-group: include every session whose ``group_id`` matches; useful
  for the group dashboard.
* per-session: a session and its descendants (recursive).

The actual graph construction is delegated to
:func:`stratoclave_atelier.fork_graph.build_fork_graph` -- this router
only collects the right :class:`Session` and :class:`Version` rows.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter

from stratoclave_atelier.api.deps import StoreDep, http_not_found
from stratoclave_atelier.api.schemas import (
    ForkGraphEdgeRead,
    ForkGraphNodeRead,
    ForkGraphResponse,
)
from stratoclave_atelier.core import NotFoundError, Session
from stratoclave_atelier.fork_graph import build_fork_graph

router = APIRouter(tags=["fork-graph"])


@router.get(
    "/api/groups/{group_id}/fork-graph",
    response_model=ForkGraphResponse,
)
async def group_fork_graph(group_id: UUID, store: StoreDep) -> ForkGraphResponse:
    try:
        await store.get_group(group_id)
    except NotFoundError as exc:
        raise http_not_found(exc) from exc

    sessions = await store.list_sessions(group_id=group_id)
    versions = []
    for session in sessions:
        versions.extend(await store.list_versions(session.session_id))

    nodes, edges = build_fork_graph(sessions, versions)
    return ForkGraphResponse(
        nodes=[ForkGraphNodeRead.from_domain(n) for n in nodes],
        edges=[ForkGraphEdgeRead.from_domain(e) for e in edges],
    )


@router.get(
    "/api/sessions/{session_id}/fork-graph",
    response_model=ForkGraphResponse,
)
async def session_fork_graph(session_id: UUID, store: StoreDep) -> ForkGraphResponse:
    try:
        root = await store.get_session(session_id)
    except NotFoundError as exc:
        raise http_not_found(exc) from exc

    # Collect root + descendants via BFS over parent_session_id.
    all_sessions = await store.list_sessions()
    by_parent: dict[UUID | None, list[Session]] = {}
    for s in all_sessions:
        by_parent.setdefault(s.parent_session_id, []).append(s)

    selected: list[Session] = [root]
    queue: list[UUID] = [root.session_id]
    seen = {root.session_id}
    while queue:
        current = queue.pop(0)
        for child in by_parent.get(current, []):
            if child.session_id in seen:
                continue
            seen.add(child.session_id)
            selected.append(child)
            queue.append(child.session_id)

    versions = []
    for s in selected:
        versions.extend(await store.list_versions(s.session_id))

    nodes, edges = build_fork_graph(selected, versions)
    return ForkGraphResponse(
        nodes=[ForkGraphNodeRead.from_domain(n) for n in nodes],
        edges=[ForkGraphEdgeRead.from_domain(e) for e in edges],
    )
