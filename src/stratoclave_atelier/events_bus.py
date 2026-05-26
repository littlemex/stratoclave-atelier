"""In-process event bus for live SSE broadcast (Stage G).

Anyone who appends to ``Store`` should also publish the resulting
:class:`Event` here so SSE subscribers and the agent runner pick it up
in real time. The bus is intentionally tiny: an asyncio fan-out queue
per session, no persistence, and ``replay`` is delegated to
``Store.list_events`` (we don't double-buffer history).

Stage F shipped a replay-only SSE handler. Stage G replaces it with a
two-phase stream: replay history first, then attach to the bus and
forward live events. Keepalive pings are emitted by the SSE handler,
not the bus, since they are transport detail.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from uuid import UUID

from stratoclave_atelier.core.types import Event


@dataclass(eq=False)
class _Subscriber:
    """Per-subscriber state: the queue plus a 'needs resync' flag.

    ``eq=False`` keeps dataclass-generated ``__eq__`` from clobbering the
    default object identity used by ``set`` membership.
    """

    queue: asyncio.Queue[Event | None]
    needs_resync: bool = field(default=False)


class EventBus:
    """Per-session asyncio fan-out for live event broadcast.

    Subscribers receive every :class:`Event` published *after* they
    subscribe. Backpressure: the queue is bounded; if a subscriber falls
    behind, the event is dropped and a ``needs_resync`` flag is set on
    the subscriber. The next successful ``put_nowait`` (after the
    consumer drains) injects a ``None`` sentinel before resuming normal
    delivery; consumers treat ``None`` as a "reconnect via replay"
    signal.
    """

    def __init__(self, *, queue_max: int = 256) -> None:
        self._queue_max = queue_max
        self._subscribers: dict[UUID, set[_Subscriber]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def publish(self, event: Event) -> None:
        async with self._lock:
            subs = list(self._subscribers.get(event.session_id, ()))
        for sub in subs:
            if sub.needs_resync:
                # Try to land the resync sentinel before any further events.
                try:
                    sub.queue.put_nowait(None)
                    sub.needs_resync = False
                except asyncio.QueueFull:
                    continue  # still backed up; keep flag set
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:
                sub.needs_resync = True

    @asynccontextmanager
    async def subscribe(self, session_id: UUID) -> AsyncIterator[asyncio.Queue[Event | None]]:
        sub = _Subscriber(queue=asyncio.Queue(maxsize=self._queue_max))
        async with self._lock:
            self._subscribers[session_id].add(sub)
        try:
            yield sub.queue
        finally:
            async with self._lock:
                bucket = self._subscribers.get(session_id)
                if bucket is not None:
                    bucket.discard(sub)
                    if not bucket:
                        self._subscribers.pop(session_id, None)


__all__ = ["EventBus"]
