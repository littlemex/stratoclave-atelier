"""SSE event stream + raw search for sessions.

Stage C added the SSE replay endpoint
(``GET /api/sessions/{id}/events?from_seq=N``). Stage G layered live
tail on top via the in-process :class:`EventBus`. Stage K adds a
sibling endpoint, ``GET /api/sessions/{id}/events/search``, that scans
turn payloads for a substring match -- the *fallback* path of the
"ask another session" feature when distill is disabled or has not
ingested the target session yet. The fallback is intentionally
linear: atelier per-session event counts are bounded (low thousands)
and the query is one-shot, so adding a Postgres trigram index would
be premature.

The stream contract (unchanged):

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
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from stratoclave_atelier.api.deps import EventBusDep, StoreDep, http_not_found
from stratoclave_atelier.api.schemas import EventRead
from stratoclave_atelier.core import NotFoundError
from stratoclave_atelier.core.types import Event, EventKind

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


class EventsSearchResponse(BaseModel):
    """Result of ``GET /api/sessions/{id}/events/search``.

    ``total_scanned`` is the count of events the handler walked before
    cutting at ``limit``; useful for the SPA to render a "showing N of
    M" hint without issuing a second request.
    """

    session_id: UUID
    query: str
    matches: list[EventRead]
    total_scanned: int = Field(..., ge=0)


def _payload_text(payload: dict[str, Any]) -> str:
    """Return the searchable text for a turn-style payload.

    The chat surface stores user / assistant turns as
    ``{"kind": "turn", "role": ..., "content": "..."}``. Tool /
    snapshot events occasionally use richer shapes; we fall back to a
    JSON dump so substring matches still hit those payloads instead of
    silently dropping them. The dump uses ``ensure_ascii=False`` so
    non-ASCII content (e.g. Japanese conversations) matches without
    callers having to escape.
    """

    content = payload.get("content")
    if isinstance(content, str):
        return content
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


@router.get("/{session_id}/events/search", response_model=EventsSearchResponse)
async def search_events(
    session_id: UUID,
    store: StoreDep,
    q: Annotated[str, Query(min_length=1, max_length=4_000)],
    kind: Annotated[EventKind | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 25,
) -> EventsSearchResponse:
    """Return up to ``limit`` events whose payload contains ``q``.

    Matching is case-insensitive substring containment over
    :func:`_payload_text`. ``kind`` (when set) filters to events of
    that kind before searching -- the SPA defaults to ``turn`` so tool
    invocations and snapshot events do not pollute the result list.
    """

    try:
        events = await store.list_events(session_id)
    except NotFoundError as exc:
        raise http_not_found(exc) from exc

    needle = q.lower()
    matches: list[Event] = []
    scanned = 0
    for event in events:
        if kind is not None and event.kind != kind:
            continue
        scanned += 1
        if needle in _payload_text(event.payload).lower():
            matches.append(event)
            if len(matches) >= limit:
                break

    return EventsSearchResponse(
        session_id=session_id,
        query=q,
        matches=[EventRead.from_domain(e) for e in matches],
        total_scanned=scanned,
    )
