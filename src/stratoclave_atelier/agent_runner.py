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
import shutil
from collections.abc import Mapping
from pathlib import Path
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
        # Stage K: one-shot memory blocks adopted by the user via the
        # cross-session "ask another session" panel. Keyed by atelier
        # session id; consumed by the next ``run()`` and cleared. Lives
        # in-process intentionally -- adoptions are inherently
        # session-bound and short-lived.
        self._pending_memory: dict[UUID, str] = {}

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

    def resolve_session_cwd(self, session_id: UUID, *, backend: str | None = None) -> str:
        """Return the on-disk cwd a given atelier session should run in.

        When :attr:`AtelierConfig.agent_cwd_isolation` is ``"per_session"``
        the cwd is ``${base}/sessions/${session_id}`` so that any state
        the backend persists alongside its cwd (Claude Code's auto-memory,
        ``.claude/projects/`` transcripts, etc.) does not leak across
        atelier sessions or across parent/child branches. ``"shared"``
        keeps the Stage G behaviour and returns the configured base
        directly.

        The returned directory is created on demand. Raises
        :class:`RuntimeError` when no base cwd is configured.
        """

        backend_name = self._resolve_backend_for(backend)
        base = self._config.cwd_for_backend(backend_name)
        if not base:
            raise RuntimeError(
                f"agent_cwd is not configured for backend {backend_name!r}; "
                f"set ATELIER_AGENT_CWD_{backend_name.upper()} or ATELIER_AGENT_CWD"
            )
        if self._config.agent_cwd_isolation == "shared":
            cwd_path = Path(base)
        else:
            cwd_path = Path(base) / "sessions" / str(session_id)
        cwd_path.mkdir(parents=True, exist_ok=True)
        return str(cwd_path)

    async def _ensure_session(
        self, session_id: UUID, *, backend: str | None = None
    ) -> AgentSession:
        async with self._lock:
            existing = self._sessions.get(session_id)
            if existing is not None:
                return existing
            backend_name = self._resolve_backend_for(backend)
            cwd = self.resolve_session_cwd(session_id, backend=backend_name)
            cfg = BackendConfig(
                backend=backend_name,
                cwd=cwd,
                allowed_tools=self._config.allowed_tools_for_backend(backend_name) or None,
            )
            session = create_session(cfg, session_id=str(session_id))
            self._sessions[session_id] = session
            return session

    async def seed_branch_cwd(
        self,
        *,
        parent_session_id: UUID,
        child_session_id: UUID,
        backend: str | None = None,
    ) -> None:
        """Copy the parent session's cwd into the child's cwd at branch time.

        The chat shell calls this once, right after
        ``POST /api/sessions/{id}/branch`` creates the child row, so the
        child inherits everything the agent had on disk up to the fork
        point (Claude memory files, project assets, scratch notes) but
        diverges from there. Subsequent writes by parent or child stay
        confined to their own per-session cwd.

        Two trees get copied:

        * The cwd subtree itself (``${base}/sessions/<sid>/``) for any
          project-local files the agent persisted.
        * The Claude Code auto-memory dir at
          ``~/.claude/projects/<encoded-cwd>/memory/``. Claude does not
          store its memory inside the cwd; it keys per-project state by
          a slug derived from the realpath of the cwd. Without this copy
          the per-session cwd isolation accidentally erases the
          parent's learned facts at fork time, which defeats the
          purpose of branching.

        We deliberately skip Claude's per-conversation ``*.jsonl``
        transcripts: those are bound to a specific Claude session id and
        copying them would resurrect a session the child should not
        replay.

        No-op when isolation is ``"shared"`` (there is only one cwd) or
        when the parent has never been warmed (no source directory to
        copy yet).
        """

        if self._config.agent_cwd_isolation != "per_session":
            return
        try:
            parent_cwd = self.resolve_session_cwd(parent_session_id, backend=backend)
            child_cwd = self.resolve_session_cwd(child_session_id, backend=backend)
        except RuntimeError:
            # No backend cwd configured -- nothing to copy.
            return
        parent_path = Path(parent_cwd)
        child_path = Path(child_cwd)
        if parent_path == child_path:
            return
        if not await asyncio.to_thread(parent_path.exists):
            return
        # ``copytree`` with ``dirs_exist_ok`` overlays the parent tree on
        # top of the freshly-created (empty) child directory. We delegate
        # to a thread to keep the event loop responsive on large cwds.
        await asyncio.to_thread(
            shutil.copytree,
            str(parent_path),
            str(child_path),
            symlinks=False,
            dirs_exist_ok=True,
        )
        await asyncio.to_thread(self._copy_claude_memory_dir, parent_path, child_path)

    @staticmethod
    def _claude_project_memory_dir(cwd: Path) -> Path:
        """Return the ``~/.claude/projects/<slug>/memory`` path for ``cwd``.

        Claude Code derives the project slug from ``realpath(cwd)`` by
        replacing every ``/`` with ``-``. On macOS that means ``/tmp``
        becomes ``-private-tmp-...`` because the realpath crosses the
        ``/private`` boundary; we mirror that resolution so the slug
        matches whatever Claude wrote.
        """

        real = cwd.resolve()
        slug = str(real).replace("/", "-")
        return Path.home() / ".claude" / "projects" / slug / "memory"

    @classmethod
    def _copy_claude_memory_dir(cls, parent_cwd: Path, child_cwd: Path) -> None:
        """Best-effort copy of Claude's per-project memory dir."""

        src = cls._claude_project_memory_dir(parent_cwd)
        dst = cls._claude_project_memory_dir(child_cwd)
        if not src.exists():
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(str(src), str(dst), symlinks=False, dirs_exist_ok=True)

    def adopt_memory(self, session_id: UUID, memory_block: str) -> None:
        """Stash a user-adopted memory block for the next ``run()``.

        Called from ``POST /api/memory/adopt`` after the cross-session
        panel resolves a query. The block survives until the next agent
        run for ``session_id`` consumes it; subsequent adoptions
        overwrite the pending block (we keep the most recent intent).
        """

        self._pending_memory[session_id] = memory_block

    def peek_pending_memory(self, session_id: UUID) -> str | None:
        """Return the pending block without clearing it (for the SPA badge)."""

        return self._pending_memory.get(session_id)

    def clear_pending_memory(self, session_id: UUID) -> str | None:
        """Pop and return the pending block, if any."""

        return self._pending_memory.pop(session_id, None)

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

        Stage K: a *user-adopted* memory block (from the cross-session
        "@" panel) takes precedence over the auto-retrieved one. The
        pending block is consumed regardless of whether
        ``agent_memory_enabled`` is true so adopt-then-disable does not
        silently swallow the user's intent.
        """

        if not self.enabled:
            raise RuntimeError(
                f"agent backend disabled; set ATELIER_AGENT_BACKEND to enable "
                f"(current={self._config.agent_backend!r})"
            )

        session = await self._ensure_session(session_id, backend=backend)

        memory_source: str | None
        if memory_context is not None:
            memory_source = "explicit"
        else:
            adopted = self.clear_pending_memory(session_id)
            if adopted is not None:
                memory_context = adopted
                memory_source = "adopted"
            elif self._config.agent_memory_enabled and self._memory.enabled:
                # Memory retrieval is best-effort and never blocks the
                # run: any failure inside the memory service is logged
                # and swallowed.
                try:
                    memory_context = await self._memory.retrieve(query=prompt)
                except Exception:
                    logger.exception("memory retrieve failed for session %s", session_id)
                    memory_context = None
                memory_source = "auto" if memory_context is not None else None
            else:
                memory_source = None

        full_prompt = self._compose_prompt(prompt, memory_context)

        # Persist the user turn first so the timeline is consistent
        # even if the agent crashes before producing any chunks.
        # ``memory_source`` tells the SPA whether the block came from
        # auto-retrieval, the cross-session adopt flow, or was passed
        # in by a CLI caller.
        await self._publish_event(
            session_id=session_id,
            kind="turn",
            payload={
                "kind": "turn",
                "role": "user",
                "content": prompt,
                "memory_used": memory_context is not None,
                "memory_source": memory_source,
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
