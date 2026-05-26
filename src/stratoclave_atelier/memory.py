"""Memory layer (Stage G): atelier <-> stratoclave-distill bridge.

Atelier persists every turn into its own ``events`` log, but freeze /
fork / replay are *session-local* operations -- they never reach across
sessions. The memory layer is the cross-session story:

* on freeze, the selected turn :class:`Event` rows are handed to distill
  so it can extract "learnings" (canonical facts, conflicts, gaps) and
  embed them;
* on a new agent run, the user prompt is used as a retrieval query and
  the top-k facts are spliced into a ``<memory>`` block before the
  prompt is sent to the agent.

Distill is an optional dependency (``stratoclave-atelier[memory]``);
when it is not installed, or when ``ATELIER_DISTILL_ENABLED`` is left
at the default ``false``, atelier wires :class:`NoopMemoryService` and
the agent runs without any cross-session context. This keeps the
in-process unit-test path free of Postgres / pgvector / LLM credentials
while still exercising the integration glue.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from stratoclave_atelier.config import AtelierConfig
from stratoclave_atelier.core.types import Event

logger = logging.getLogger(__name__)


class MemoryService(Protocol):
    """Cross-session memory abstraction owned by atelier.

    Implementations must be tolerant of "missing data" -- the atelier
    server starts up before any session is frozen, and the SPA may run
    queries against sessions that have not yet contributed to memory.
    """

    @property
    def enabled(self) -> bool:
        """``True`` if calls actually reach distill; ``False`` for noop."""

    async def ingest_session(
        self,
        *,
        session_id: UUID,
        events: Sequence[Event],
    ) -> None:
        """Hand a frozen run of turn events to distill for ingestion.

        ``session_id`` is the atelier session id (the source-of-truth in
        the events log); implementations are expected to pass it through
        as the distill ``session_id`` so :meth:`retrieve` can attribute
        facts back to the session that produced them. Only events with
        ``kind == "turn"`` are considered; the caller may pass the
        unfiltered slice and let the implementation filter.
        """

    async def retrieve(self, *, query: str, top_k: int = 5) -> str | None:
        """Return a ``<memory>``-ready string for ``query`` or ``None``.

        ``None`` means "no useful context" (either disabled, no
        learnings yet, or the retriever returned nothing); the caller is
        expected to skip the ``<memory>`` block in that case.
        """

    async def aclose(self) -> None:
        """Release any owned resources (DB pools, HTTP clients)."""


class NoopMemoryService(MemoryService):
    """Default implementation when distill is disabled.

    All calls are no-ops; ``retrieve`` returns ``None`` so the agent
    runner skips the ``<memory>`` block entirely. The class is the
    explicit, testable replacement for "is distill installed?" branching
    sprinkled across the codebase.
    """

    enabled = False

    async def ingest_session(
        self,
        *,
        session_id: UUID,
        events: Sequence[Event],
    ) -> None:
        return None

    async def retrieve(self, *, query: str, top_k: int = 5) -> str | None:
        return None

    async def aclose(self) -> None:
        return None


async def build_memory_service(config: AtelierConfig) -> MemoryService:
    """Wire a memory service from config.

    Returns :class:`NoopMemoryService` unless distill is both installed
    and enabled via ``ATELIER_DISTILL_ENABLED``. Import errors on the
    distill path are logged and demoted to noop so a server with the
    extra missing degrades gracefully instead of failing to boot. The
    coroutine awaits the distill pool open so callers must call this
    from an async context (the FastAPI lifespan does, by construction).
    """

    if not config.distill_enabled:
        return NoopMemoryService()
    try:
        from stratoclave_atelier._distill_memory import (
            DistillMemoryService,
        )
    except ImportError as exc:
        logger.warning(
            "ATELIER_DISTILL_ENABLED is true but stratoclave-distill is not "
            "installed (%s); falling back to NoopMemoryService. Install with "
            "'pip install stratoclave-atelier[memory]'.",
            exc,
        )
        return NoopMemoryService()
    return await DistillMemoryService.from_config(config)


__all__ = [
    "MemoryService",
    "NoopMemoryService",
    "build_memory_service",
]
