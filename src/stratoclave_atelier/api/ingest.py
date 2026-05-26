"""WebSocket ingest endpoint for Stage C.

Loom (or any agent runtime) opens
``WS /api/sessions/{session_id}/ingest`` and streams JSONL turns -- one
JSON object per WebSocket text message. Each message is

* validated as JSON,
* appended to the session's event log via
  :meth:`Store.append_event`, and
* echoed back to the client as ``{"type": "ack", "seq": N}`` so the
  sender can drive its own retry loop.

Errors are signalled by ``{"type": "error", "code": ..., "detail": ...}``
and -- depending on severity -- close the socket. The WebSocket close
codes follow RFC 6455:

* ``1003 (unsupported data)`` -- non-text or non-JSON frame.
* ``1008 (policy violation)`` -- session is frozen / archived.
* ``1011 (internal error)`` -- store / DB failure.
* ``4404`` -- application-defined: target session does not exist.

The handler does **not** persist a JSONL spool here -- the raw turn
already lives in ``events.payload`` and freeze (Stage C-3) re-emits the
JSONL by replaying the events. Keeping a single source of truth means
freeze can happen on any turn range without reconciling two stores.
"""

from __future__ import annotations

import json
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from stratoclave_atelier.api.deps import get_event_bus, get_store
from stratoclave_atelier.core import ConflictError, NotFoundError
from stratoclave_atelier.db import Store
from stratoclave_atelier.events_bus import EventBus

router = APIRouter(tags=["ingest"])

# Application-defined close codes (>= 4000 per RFC 6455 section 7.4.2).
_CLOSE_SESSION_NOT_FOUND = 4404
_CLOSE_SESSION_FROZEN = 4423


@router.websocket("/api/sessions/{session_id}/ingest")
async def ingest_session(websocket: WebSocket, session_id: UUID) -> None:
    """Stream JSONL turns into a session's event log."""

    store: Store = get_store(websocket)  # type: ignore[arg-type]
    bus: EventBus = get_event_bus(websocket)  # type: ignore[arg-type]
    await websocket.accept()

    try:
        session = await store.get_session(session_id)
    except NotFoundError as exc:
        await websocket.close(code=_CLOSE_SESSION_NOT_FOUND, reason=str(exc))
        return

    if session.status != "active":
        await websocket.close(
            code=_CLOSE_SESSION_FROZEN,
            reason=f"session status is {session.status!r}, not 'active'",
        )
        return

    try:
        while True:
            try:
                raw = await websocket.receive_text()
            except WebSocketDisconnect:
                return

            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                await websocket.send_json(
                    {"type": "error", "code": "invalid_json", "detail": str(exc)}
                )
                await websocket.close(
                    code=status.WS_1003_UNSUPPORTED_DATA,
                    reason="payload was not valid JSON",
                )
                return

            if not isinstance(payload, dict):
                await websocket.send_json(
                    {
                        "type": "error",
                        "code": "invalid_payload",
                        "detail": "ingest expects a JSON object per message",
                    }
                )
                await websocket.close(
                    code=status.WS_1003_UNSUPPORTED_DATA,
                    reason="payload was not a JSON object",
                )
                return

            kind = payload.get("kind", "turn")
            if kind not in ("turn", "freeze", "fork", "system"):
                kind = "turn"

            try:
                event = await store.append_event(
                    session_id=session_id,
                    kind=kind,
                    payload=payload,
                )
            except NotFoundError as exc:
                await websocket.close(code=_CLOSE_SESSION_NOT_FOUND, reason=str(exc))
                return
            except ConflictError as exc:
                await websocket.send_json({"type": "error", "code": "conflict", "detail": str(exc)})
                await websocket.close(code=_CLOSE_SESSION_FROZEN, reason=str(exc))
                return

            await bus.publish(event)
            await websocket.send_json(
                {
                    "type": "ack",
                    "seq": event.seq,
                    "event_id": str(event.event_id),
                }
            )
    except WebSocketDisconnect:
        return
