"""SSE replay endpoint for Stage C.

``GET /api/sessions/{id}/events?from_seq=N`` streams the historical
event log as Server-Sent Events. The stream:

1. Yields each persisted event as ``id: <seq>\\nevent: <kind>\\ndata: <json>\\n\\n``.
2. Closes when the historical replay is exhausted -- this is a
   *replay* endpoint, not a live tail. (Live tail will land later via
   Postgres ``LISTEN/NOTIFY`` or a redis-backed bus.)

Why "replay only" for now: the WebSocket ingest endpoint
(:mod:`stratoclave_atelier.api.ingest`) already gives consumers a
synchronous ack stream, so the SSE channel only needs to backfill state
on UI reconnect. Adding live tail later is purely additive.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from stratoclave_atelier.api.deps import StoreDep, http_not_found
from stratoclave_atelier.core import NotFoundError
from stratoclave_atelier.core.types import Event

router = APIRouter(prefix="/api/sessions", tags=["events"])


def _format_sse(event: Event) -> str:
    """Format a domain :class:`Event` as a single SSE message frame."""

    data = json.dumps(
        {
            "event_id": str(event.event_id),
            "session_id": str(event.session_id),
            "seq": event.seq,
            "kind": event.kind,
            "payload": event.payload,
            "created_at": event.created_at.isoformat(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"id: {event.seq}\nevent: {event.kind}\ndata: {data}\n\n"


@router.get("/{session_id}/events")
async def replay_events(
    session_id: UUID,
    store: StoreDep,
    from_seq: Annotated[int, Query(ge=0)] = 0,
) -> StreamingResponse:
    """Stream events ``[from_seq..MAX]`` as SSE."""

    try:
        events = await store.list_events(session_id, from_seq=from_seq)
    except NotFoundError as exc:
        raise http_not_found(exc) from exc

    async def emit() -> AsyncIterator[bytes]:
        for event in events:
            yield _format_sse(event).encode("utf-8")

    return StreamingResponse(
        emit(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
