"""Stage L Curator: an isolated agent that answers questions about a scope.

The Curator is the spiritual replacement for the cross-session ``@``
mention panel. Instead of pasting a retrieved memory block into the
*main* agent (which contaminates its working memory), the operator
opens the Curator panel, picks a scope (a Group or a session-and-its-
ancestors), picks a context mode (distilled summary or raw events),
and asks a question. The Curator spawns a *separate* agent session in
its own per-query cwd so the answer cannot leak back into the active
chat.

The cwd lives at ``${agent_cwd}/curators/<query_id>`` and is created
fresh for every query. The Claude Code auto-memory directory keyed by
``realpath(cwd)`` is therefore empty by construction, which is what we
want: the Curator must not inherit cross-session learnings -- the
scope text is the only context it sees.

The Curator is intentionally fire-and-forget: callers ``await
curate()`` directly and consume the streaming chunks. There is no
warm-pool; once the answer streams to completion the underlying loom
session is closed and the cwd left in place for inspection (the cwd
is small -- only the system prompt and the loom transcripts).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from stratoclave_loom import (
    AcpChunk,
    BackendConfig,
    create_session,
)

from stratoclave_atelier.config import AtelierConfig
from stratoclave_atelier.core import NotFoundError
from stratoclave_atelier.core.types import Event, Session
from stratoclave_atelier.db import Store
from stratoclave_atelier.memory import MemoryService

logger = logging.getLogger(__name__)


CuratorContextMode = Literal["distill", "raw"]
CuratorScopeKind = Literal["group", "session"]


SYSTEM_PROMPT_TEMPLATE = """You are the Curator, a read-only assistant
embedded in stratoclave-atelier. Your job is to answer the operator's
question using ONLY the context block below. Do not speculate beyond
the context. If the context does not contain enough information, say
so plainly.

Identify yourself as "Curator" if asked. Keep answers concise and
factual; cite specific events, sessions, or versions when relevant.

--- BEGIN CONTEXT ---
{context}
--- END CONTEXT ---
""".strip()


class CuratorScopeError(ValueError):
    """Raised when the requested scope cannot be resolved."""


class CuratorContextError(RuntimeError):
    """Raised when the requested context mode is unavailable.

    Currently only the ``distill`` mode can fail this way -- when distill
    is not enabled on the server we cannot synthesise a summary block.
    """


async def resolve_session_chain(store: Store, session_id: UUID) -> list[Session]:
    """Return the session and every ancestor up to the root.

    The chain is returned root-first, mirroring the breadcrumb the SPA
    renders. Walk via ``parent_session_id`` because that is what the
    fork-graph uses; a missing parent (e.g. the row was deleted)
    truncates the chain rather than raising.
    """

    chain: list[Session] = []
    current_id: UUID | None = session_id
    while current_id is not None:
        try:
            current = await store.get_session(current_id)
        except NotFoundError:
            break
        chain.append(current)
        current_id = current.parent_session_id
    return list(reversed(chain))


async def resolve_scope_sessions(
    store: Store,
    *,
    scope_kind: CuratorScopeKind,
    scope_id: UUID,
) -> list[Session]:
    """Return every :class:`Session` covered by the requested scope.

    * ``group`` -- every session whose ``group_id`` matches.
    * ``session`` -- the target session and its ancestors back to the
      root, so the Curator sees the full conversation history that
      led to ``scope_id``.
    """

    if scope_kind == "group":
        try:
            await store.get_group(scope_id)
        except NotFoundError as exc:
            raise CuratorScopeError(f"group {scope_id} not found") from exc
        return await store.list_sessions(group_id=scope_id)
    if scope_kind == "session":
        chain = await resolve_session_chain(store, scope_id)
        if not chain:
            raise CuratorScopeError(f"session {scope_id} not found")
        return chain
    raise CuratorScopeError(f"unsupported scope_kind: {scope_kind!r}")


async def build_distill_context(
    memory: MemoryService,
    *,
    sessions: Sequence[Session],
    question: str,
) -> str:
    """Render the distilled-memory context block.

    Falls back to a "no matches" string when the retriever returns
    nothing, so the Curator still sees a coherent prompt rather than an
    empty ``<memory>`` tag.
    """

    if not memory.enabled:
        raise CuratorContextError(
            "distill memory is disabled on this server; pick raw context mode"
        )
    scope_ids = [s.session_id for s in sessions]
    block = await memory.retrieve(
        query=question,
        top_k=10,
        scope_session_ids=scope_ids if scope_ids else None,
    )
    if not block:
        return (
            "(no distilled memory matched this scope; the Curator was given an empty memory block.)"
        )
    return block


async def build_raw_context(
    store: Store,
    *,
    sessions: Sequence[Session],
    max_events_per_session: int = 200,
) -> str:
    """Render the raw events context block.

    Each session is dumped chronologically, capped at
    ``max_events_per_session`` events to keep the prompt budget
    manageable. Only ``turn`` and ``agent_turn`` payloads are included
    -- the streaming ``agent_chunk`` rows are noise once the per-turn
    summary exists.
    """

    blocks: list[str] = []
    for session in sessions:
        events = await store.list_events(session.session_id)
        kept: list[Event] = [e for e in events if e.kind in ("turn", "agent_turn")]
        kept = kept[-max_events_per_session:]
        lines = [f"## session {session.title or session.session_id} ({session.session_id})"]
        for ev in kept:
            payload = ev.payload or {}
            role = payload.get("role") or ev.kind
            content = payload.get("content")
            if not isinstance(content, str):
                content = str(content) if content is not None else ""
            lines.append(f"[seq {ev.seq}] {role}: {content.strip()}")
        if len(kept) == 0:
            lines.append("(no turn events)")
        blocks.append("\n".join(lines))
    if not blocks:
        return "(scope had no sessions)"
    return "\n\n".join(blocks)


def render_system_prompt(context: str) -> str:
    return SYSTEM_PROMPT_TEMPLATE.format(context=context)


class CuratorRunner:
    """Spawn isolated agent sessions that answer scope-bound questions.

    The runner keeps no warm-pool: each ``curate()`` call builds a
    fresh cwd, asks the question, streams chunks, and tears the loom
    session down. The owner ID for the cwd is the ``query_id`` we mint
    on entry so concurrent queries cannot collide.
    """

    def __init__(
        self,
        *,
        config: AtelierConfig,
        store: Store,
        memory: MemoryService,
    ) -> None:
        self._config = config
        self._store = store
        self._memory = memory

    @property
    def enabled(self) -> bool:
        """``True`` when at least one agent backend is configured.

        We piggyback on the same backend list :class:`AgentRunner` uses;
        a server with no backend cannot run Curator queries either.
        """

        return self._config.agent_backend != "none" or bool(self._config.resolved_backends())

    def _resolve_backend(self, requested: str | None) -> str:
        if requested:
            return requested
        if self._config.agent_backend != "none":
            return self._config.agent_backend
        allowed = self._config.resolved_backends()
        if len(allowed) == 1:
            return allowed[0]
        raise CuratorContextError(
            "no default agent backend; "
            "set ATELIER_AGENT_BACKEND or pass agent_backend on the request"
        )

    def _resolve_query_cwd(self, *, backend: str, query_id: UUID) -> str:
        """Return a fresh per-query cwd, creating the directory eagerly.

        The path is ``${agent_cwd}/curators/<query_id>`` so it lives on
        the same filesystem as the regular session cwds (lets us reuse
        the operator's volume mounts) but in a separate subtree the
        chat code never touches.
        """

        base = self._config.cwd_for_backend(backend)
        if not base:
            raise CuratorContextError(
                f"agent_cwd is not configured for backend {backend!r}; "
                f"set ATELIER_AGENT_CWD_{backend.upper()} or ATELIER_AGENT_CWD"
            )
        path = Path(base) / "curators" / str(query_id)
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    async def curate(
        self,
        *,
        scope_kind: CuratorScopeKind,
        scope_id: UUID,
        context_mode: CuratorContextMode,
        question: str,
        backend: str | None = None,
    ) -> AsyncIterator[AcpChunk]:
        """Stream the Curator's answer for ``question`` against the scope.

        The caller iterates the returned async iterator until exhausted;
        the loom session is closed in the ``finally`` branch so even
        cancellations clean up the cwd.
        """

        if not self.enabled:
            raise CuratorContextError(
                f"agent backend disabled; set ATELIER_AGENT_BACKEND to enable "
                f"(current={self._config.agent_backend!r})"
            )

        backend_name = self._resolve_backend(backend)
        sessions = await resolve_scope_sessions(
            self._store, scope_kind=scope_kind, scope_id=scope_id
        )

        if context_mode == "distill":
            context = await build_distill_context(
                self._memory, sessions=sessions, question=question
            )
        elif context_mode == "raw":
            context = await build_raw_context(self._store, sessions=sessions)
        else:
            raise CuratorContextError(f"unsupported context_mode: {context_mode!r}")

        query_id = uuid4()
        cwd = self._resolve_query_cwd(backend=backend_name, query_id=query_id)
        cfg = BackendConfig(
            backend=backend_name,
            cwd=cwd,
            allowed_tools=self._config.allowed_tools_for_backend(backend_name) or None,
        )
        loom_session = create_session(cfg, session_id=str(query_id))

        async def _stream() -> AsyncIterator[AcpChunk]:
            try:
                stream = await loom_session.send_message(
                    f"{render_system_prompt(context)}\n\nOperator question:\n{question}"
                )
                async for chunk in stream:
                    yield chunk
            finally:
                try:
                    await loom_session.close()
                except Exception:
                    logger.exception("error closing curator session %s", query_id)

        return _stream()


__all__ = [
    "CuratorContextError",
    "CuratorContextMode",
    "CuratorRunner",
    "CuratorScopeError",
    "CuratorScopeKind",
    "build_distill_context",
    "build_raw_context",
    "render_system_prompt",
    "resolve_scope_sessions",
    "resolve_session_chain",
]
