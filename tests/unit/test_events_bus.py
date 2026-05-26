"""Unit tests for the in-process :class:`EventBus`."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from stratoclave_atelier.core.types import Event
from stratoclave_atelier.events_bus import EventBus


def _make_event(session_id: object, seq: int) -> Event:
    return Event(
        event_id=uuid4(),
        session_id=session_id,  # type: ignore[arg-type]
        seq=seq,
        kind="turn",
        payload={"i": seq},
        created_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_publish_delivers_to_active_subscriber() -> None:
    bus = EventBus()
    sid = uuid4()

    async with bus.subscribe(sid) as queue:
        await bus.publish(_make_event(sid, 0))
        item = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert item is not None
        assert item.seq == 0


@pytest.mark.asyncio
async def test_publish_isolates_sessions() -> None:
    bus = EventBus()
    sid_a, sid_b = uuid4(), uuid4()

    async with bus.subscribe(sid_a) as qa, bus.subscribe(sid_b) as qb:
        await bus.publish(_make_event(sid_a, 0))
        a = await asyncio.wait_for(qa.get(), timeout=1.0)
        assert a is not None and a.seq == 0
        # qb stays empty
        assert qb.empty()


@pytest.mark.asyncio
async def test_subscribe_does_not_replay_prior_events() -> None:
    bus = EventBus()
    sid = uuid4()
    await bus.publish(_make_event(sid, 0))

    async with bus.subscribe(sid) as queue:
        # No subscriber existed when seq=0 was published, so nothing arrives.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(queue.get(), timeout=0.05)


@pytest.mark.asyncio
async def test_unsubscribe_removes_session_bucket() -> None:
    bus = EventBus()
    sid = uuid4()

    async with bus.subscribe(sid):
        pass

    # Publishing after the subscriber leaves must be a no-op (not crash).
    await bus.publish(_make_event(sid, 0))


@pytest.mark.asyncio
async def test_backpressure_signals_resync_with_none() -> None:
    bus = EventBus(queue_max=2)
    sid = uuid4()

    async with bus.subscribe(sid) as queue:
        # Fill the queue, then overflow without draining: the third
        # publish marks the subscriber for resync rather than blocking.
        await bus.publish(_make_event(sid, 0))
        await bus.publish(_make_event(sid, 1))
        await bus.publish(_make_event(sid, 2))  # dropped, sets needs_resync

        # Drain the buffered events and then publish a new one: the
        # sentinel should be injected ahead of seq=3.
        first = await asyncio.wait_for(queue.get(), timeout=1.0)
        second = await asyncio.wait_for(queue.get(), timeout=1.0)
        await bus.publish(_make_event(sid, 3))

        sentinel = await asyncio.wait_for(queue.get(), timeout=1.0)
        third = await asyncio.wait_for(queue.get(), timeout=1.0)

        assert first is not None and first.seq == 0
        assert second is not None and second.seq == 1
        assert sentinel is None
        assert third is not None and third.seq == 3
