"""``/api/sessions/{id}/snapshot-query`` REST router.

Stage D introduces cross-session questions against a frozen
:class:`Version`. The handler:

1. Validates the source session and target version exist.
2. Calls :class:`SnapshotResolver.resolve` to compute a response.
3. Persists both the query and the response via
   :meth:`Store.create_snapshot_query`.
4. Returns the audit row.

A companion ``GET /api/snapshot-queries`` (filterable by source session
or target version) lets the UI display "which sessions referenced this
version?".
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, status

from stratoclave_atelier.api.deps import (
    BlobStoreDep,
    SnapshotResolverDep,
    StoreDep,
    http_not_found,
)
from stratoclave_atelier.api.schemas import (
    SnapshotQueryCreate,
    SnapshotQueryRead,
)
from stratoclave_atelier.core import NotFoundError

router = APIRouter(tags=["snapshot-queries"])


@router.post(
    "/api/sessions/{session_id}/snapshot-query",
    response_model=SnapshotQueryRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_snapshot_query(
    session_id: UUID,
    payload: SnapshotQueryCreate,
    store: StoreDep,
    blob_store: BlobStoreDep,
    resolver: SnapshotResolverDep,
) -> SnapshotQueryRead:
    try:
        await store.get_session(session_id)
        version = await store.get_version(payload.target_version_id)
    except NotFoundError as exc:
        raise http_not_found(exc) from exc

    response = await resolver.resolve(
        store=store,
        blob_store=blob_store,
        version=version,
        query=payload.query,
    )
    row = await store.create_snapshot_query(
        source_session_id=session_id,
        target_version_id=payload.target_version_id,
        query=payload.query,
        response=response,
    )
    return SnapshotQueryRead.from_domain(row)


@router.get(
    "/api/snapshot-queries",
    response_model=list[SnapshotQueryRead],
)
async def list_snapshot_queries(
    store: StoreDep,
    source_session_id: Annotated[UUID | None, Query()] = None,
    target_version_id: Annotated[UUID | None, Query()] = None,
) -> list[SnapshotQueryRead]:
    rows = await store.list_snapshot_queries(
        source_session_id=source_session_id,
        target_version_id=target_version_id,
    )
    return [SnapshotQueryRead.from_domain(r) for r in rows]
