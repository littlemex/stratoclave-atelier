"""SSE event stream for sessions (Stage C replay + Stage G live tail).

``GET /api/sessions/{id}/events?from_seq=N`` streams the historical
event log followed by a live tail driven by the in-process
:class:`EventBus`. The stream:

1. Replays each persisted event ``[from_seq..]`` as
   ``id: <seq>\\nevent: <kind>\\ndata: <json>\\n\\n``.
2. Switches to live mode and forwards every new event published to
   the bus for this ``session_id``.
3. Sends a ``: ping\\n\\n`` keepalive every 15 seconds so reverse
   proxies do not idle-close the connection.

If the bus signals "you fell behind" (``None`` sentinel from the
queue) the handler closes the response so the client reconnects with
``from_seq=last_seq`` and resyncs through replay.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from stratoclave_atelier.api.deps import EventBusDep, StoreDep, http_not_found
from stratoclave_atelier.core import NotFoundError
from stratoclave_atelier.core.types import Event

router = APIRouter(prefix="/api/sessions", tags=["events"])

_KEEPALIVE_INTERVAL_S = 15.0


def _format_sse(event: Event) -> str:
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
async def stream_events(
    session_id: UUID,
    store: StoreDep,
    bus: EventBusDep,
    from_seq: Annotated[int, Query(ge=0)] = 0,
    follow: Annotated[bool, Query()] = False,
) -> StreamingResponse:
    """Stream events ``[from_seq..]`` and optionally follow live.

    The session is validated up front so an unknown ID maps to a clean
    404. To avoid missing events that arrive between the historical
    read and the live attach, we subscribe to the bus *first* and then
    fetch the history; replayed events are deduplicated against the
    live queue via the ``last_seen_seq`` cursor.
    """

    try:
        await store.get_session(session_id)
    except NotFoundError as exc:
        raise http_not_found(exc) from exc

    async def emit() -> AsyncIterator[bytes]:
        async with bus.subscribe(session_id) as queue:
            history = await store.list_events(session_id, from_seq=from_seq)

            last_seen_seq = from_seq - 1
            for event in history:
                yield _format_sse(event).encode("utf-8")
                last_seen_seq = event.seq

            if not follow:
                return

            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=_KEEPALIVE_INTERVAL_S)
                except TimeoutError:
                    yield b": ping\n\n"
                    continue

                if item is None:
                    return  # backpressure resync signal

                if item.seq <= last_seen_seq:
                    continue
                last_seen_seq = item.seq
                yield _format_sse(item).encode("utf-8")

    return StreamingResponse(
        emit(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
