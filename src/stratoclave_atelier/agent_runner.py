"""Stage G AgentRunner: drive a stratoclave-loom session from atelier.

The runner spawns a per-atelier-session loom :class:`AgentSession`
lazily on first ``run()`` call, streams chunks back, and persists each
chunk to the atelier event log. Persistence and SSE broadcast happen
chunk-by-chunk so the SPA sees text deltas as soon as the agent
produces them. A summary ``agent_turn`` event is appended at end of
turn so freeze/replay sees the full assistant response as one row.

Sessions are kept warm in a process-local dict keyed by
atelier session_id. Calling :meth:`AgentRunner.close` (or app shutdown)
disposes them. Cancellation is delegated to loom via
:meth:`AgentSession.cancel`.

The runner is *backend-agnostic*: it only depends on the
:mod:`stratoclave_loom` public API. Atelier picks the backend via the
``ATELIER_AGENT_BACKEND`` env knob (resolved by :class:`AtelierConfig`).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from stratoclave_loom import (
    AcpChunk,
    AgentSession,
    BackendConfig,
    create_session,
)

from stratoclave_atelier.config import AtelierConfig
from stratoclave_atelier.core.types import Event
from stratoclave_atelier.db import Store
from stratoclave_atelier.events_bus import EventBus
from stratoclave_atelier.memory import MemoryService, NoopMemoryService

logger = logging.getLogger(__name__)


class AgentRunner:
    """Owns one loom :class:`AgentSession` per atelier session_id."""

    def __init__(
        self,
        *,
        config: AtelierConfig,
        store: Store,
        bus: EventBus,
        memory: MemoryService | None = None,
    ) -> None:
        self._config = config
        self._store = store
        self._bus = bus
        self._memory: MemoryService = memory if memory is not None else NoopMemoryService()
        self._sessions: dict[UUID, AgentSession] = {}
        self._lock = asyncio.Lock()
        # Strong refs to in-flight asyncio tasks so they survive GC while
        # the SSE stream consumes their output.
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def enabled(self) -> bool:
        """``True`` when at least one backend is wired into this server.

        Stage G used the singular ``agent_backend != 'none'`` test;
        Stage H widens that to "any backend in the allowed list", so
        deployments that only set ``ATELIER_AGENT_BACKENDS_ALLOWED``
        still report enabled even when ``agent_backend`` is ``'none'``.
        """

        return self._config.agent_backend != "none" or bool(self._config.resolved_backends())

    def _resolve_backend_for(self, requested: str | None) -> str:
        """Pick the backend a session should run against.

        Falls back to ``agent_backend`` (Stage G default) when the
        session does not specify one. Raises :class:`RuntimeError` when
        nothing is configured -- the API layer surfaces that as 503.
        """

        if requested:
            return requested
        if self._config.agent_backend != "none":
            return self._config.agent_backend
        allowed = self._config.resolved_backends()
        if len(allowed) == 1:
            return allowed[0]
        raise RuntimeError(
            "no default agent backend; "
            "set ATELIER_AGENT_BACKEND or pass agent_backend on session creation"
        )

    async def _ensure_session(
        self, session_id: UUID, *, backend: str | None = None
    ) -> AgentSession:
        async with self._lock:
            existing = self._sessions.get(session_id)
            if existing is not None:
                return existing
            backend_name = self._resolve_backend_for(backend)
            cwd = self._config.cwd_for_backend(backend_name)
            if not cwd:
                raise RuntimeError(
                    f"agent_cwd is not configured for backend {backend_name!r}; "
                    f"set ATELIER_AGENT_CWD_{backend_name.upper()} or ATELIER_AGENT_CWD"
                )
            cfg = BackendConfig(
                backend=backend_name,
                cwd=cwd,
                allowed_tools=self._config.allowed_tools_for_backend(backend_name) or None,
            )
            session = create_session(cfg, session_id=str(session_id))
            self._sessions[session_id] = session
            return session

    async def run(
        self,
        *,
        session_id: UUID,
        prompt: str,
        memory_context: str | None = None,
        backend: str | None = None,
    ) -> None:
        """Send ``prompt`` to the agent and stream chunks into the event log.

        ``memory_context`` is prepended as a ``<memory>`` block when set;
        the SPA shows a "Memory: N items" badge based on the count
        encoded in the inbound user-turn payload. Errors raised by the
        backend are persisted as ``agent_error`` events and surfaced to
        the caller. ``backend`` overrides the server-default loom
        backend at session-warmup time (Stage H per-session selection);
        once a session is warm the choice is sticky.
        """

        if not self.enabled:
            raise RuntimeError(
                f"agent backend disabled; set ATELIER_AGENT_BACKEND to enable "
                f"(current={self._config.agent_backend!r})"
            )

        session = await self._ensure_session(session_id, backend=backend)

        # Memory retrieval is best-effort and never blocks the run: any
        # failure inside the memory service is logged and swallowed.
        if memory_context is None and self._config.agent_memory_enabled and self._memory.enabled:
            try:
                memory_context = await self._memory.retrieve(query=prompt)
            except Exception:
                logger.exception("memory retrieve failed for session %s", session_id)
                memory_context = None

        full_prompt = self._compose_prompt(prompt, memory_context)

        # Persist the user turn first so the timeline is consistent even
        # if the agent crashes before producing any chunks.
        await self._publish_event(
            session_id=session_id,
            kind="turn",
            payload={
                "kind": "turn",
                "role": "user",
                "content": prompt,
                "memory_used": memory_context is not None,
            },
        )

        text_buffer: list[str] = []
        try:
            stream = await session.send_message(full_prompt)
            async for chunk in stream:
                await self._handle_chunk(session_id, chunk, text_buffer)
        except Exception as exc:
            logger.exception("agent run failed for session %s", session_id)
            await self._publish_event(
                session_id=session_id,
                kind="agent_error",
                payload={"error": str(exc), "type": exc.__class__.__name__},
            )
            return

        # End-of-turn summary: one event carrying the full assistant text.
        await self._publish_event(
            session_id=session_id,
            kind="agent_turn",
            payload={
                "kind": "agent_turn",
                "role": "assistant",
                "content": "".join(text_buffer),
            },
        )

    def schedule(
        self,
        *,
        session_id: UUID,
        prompt: str,
        memory_context: str | None = None,
        backend: str | None = None,
    ) -> asyncio.Task[None]:
        """Fire-and-forget wrapper around :meth:`run` that retains the task."""

        task = asyncio.create_task(
            self.run(
                session_id=session_id,
                prompt=prompt,
                memory_context=memory_context,
                backend=backend,
            )
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def cancel(self, session_id: UUID) -> None:
        async with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            return
        await session.cancel()

    async def close(self, session_id: UUID | None = None) -> None:
        async with self._lock:
            if session_id is None:
                victims = list(self._sessions.items())
                self._sessions.clear()
            else:
                victims = (
                    [(session_id, self._sessions.pop(session_id))]
                    if session_id in self._sessions
                    else []
                )
        for sid, session in victims:
            try:
                await session.close()
            except Exception:
                logger.exception("error closing agent session %s", sid)

    async def _handle_chunk(
        self,
        session_id: UUID,
        chunk: AcpChunk,
        text_buffer: list[str],
    ) -> None:
        if chunk.chunk_type == "text_delta":
            text = str(chunk.content.get("text", ""))
            if text:
                text_buffer.append(text)
            await self._publish_event(
                session_id=session_id,
                kind="agent_chunk",
                payload={
                    "chunk_type": "text_delta",
                    "text": text,
                },
            )
            return
        if chunk.chunk_type == "end_turn":
            return
        if chunk.chunk_type == "error":
            await self._publish_event(
                session_id=session_id,
                kind="agent_error",
                payload=dict(chunk.content),
            )
            return
        # tool_use / tool_result / thought / permission_request
        await self._publish_event(
            session_id=session_id,
            kind="agent_chunk",
            payload={"chunk_type": chunk.chunk_type, **dict(chunk.content)},
        )

    async def _publish_event(
        self,
        *,
        session_id: UUID,
        kind: str,
        payload: Mapping[str, Any],
    ) -> Event:
        from typing import cast as _cast

        from stratoclave_atelier.core.types import EventKind

        event = await self._store.append_event(
            session_id=session_id,
            kind=_cast(EventKind, kind),
            payload=dict(payload),
        )
        await self._bus.publish(event)
        return event

    @staticmethod
    def _compose_prompt(prompt: str, memory_context: str | None) -> str:
        if not memory_context:
            return prompt
        return f"<memory>\n{memory_context}\n</memory>\n\n{prompt}"


__all__ = ["AgentRunner"]
