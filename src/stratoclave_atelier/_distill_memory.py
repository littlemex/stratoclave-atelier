"""Optional distill-backed :class:`MemoryService` implementation.

Imported lazily by :func:`stratoclave_atelier.memory.build_memory_service`
so the stratoclave-distill dependency stays optional. When
``ATELIER_DISTILL_ENABLED`` is true and the ``[memory]`` extra is
installed, this module wires:

* an :class:`IngestRunner` with the asyncpg store quartet (watermark,
  purpose, digest, learning) plus a real :class:`Distiller` /
  :class:`Curator`;
* a :class:`Retriever` reusing the same :class:`LearningStore` for the
  query-time lookup.

The same connection pool is shared across both directions so a single
``ATELIER_DISTILL_DATABASE_URL`` is enough to drive memory. Pool
construction is async, so :meth:`DistillMemoryService.from_config` is a
coroutine -- call it from the FastAPI lifespan (already async).

Atelier hands distill plain :class:`Event` rows. The mapping from
``payload`` to :class:`NormalizedTurn` is deliberately conservative:

* ``role`` is read from the payload (atelier always writes it for turn
  events; defaults to ``"user"`` if missing);
* ``text_content`` falls back to a JSON dump of the payload so weird
  shapes still reach distill rather than silently dropping;
* ``occurred_at`` is sourced from :attr:`Event.created_at` so the
  watermark can advance even when the agent did not stamp a turn time.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Sequence
from typing import Any
from uuid import UUID

from stratoclave_atelier.config import AtelierConfig
from stratoclave_atelier.core.errors import ConfigError
from stratoclave_atelier.core.types import Event

logger = logging.getLogger(__name__)


def _event_to_normalized_turn(event: Event) -> Any:
    """Lift an atelier turn :class:`Event` into a distill ``NormalizedTurn``."""

    from stratoclave_distill.core.types import NormalizedTurn

    payload = event.payload or {}
    role = str(payload.get("role") or "user")
    content = payload.get("content")
    text_content = str(content) if content is not None else json.dumps(payload, sort_keys=True)
    occurred_at = event.created_at.isoformat().replace("+00:00", "Z")
    return NormalizedTurn(
        turn_id=str(event.event_id),
        session_id=str(event.session_id),
        seq=event.seq,
        role=role,
        text_content=text_content,
        tool_name=None,
        tool_input=None,
        occurred_at=occurred_at,
        raw_line=json.dumps(payload, sort_keys=True),
    )


def _format_retrieval(result: Any) -> str | None:
    """Render a distill ``RetrievalResult`` as a ``<memory>``-ready string.

    Empty (no hits in either lane and no conflicts / gaps) returns
    ``None`` so the caller skips the ``<memory>`` block entirely.
    """

    lines: list[str] = []
    for hit in getattr(result, "canonical", ()):
        rule = getattr(hit.learning, "rule", "")
        if rule:
            lines.append(f"[canonical] {rule}")
    for hit in getattr(result, "emerging", ()):
        rule = getattr(hit.learning, "rule", "")
        if rule:
            lines.append(f"[emerging] {rule}")
    for conflict in getattr(result, "conflicts", ()):
        reason = getattr(conflict, "reason", "")
        if reason:
            lines.append(f"[conflict] {reason}")
    for gap in getattr(result, "gaps", ()):
        topic = getattr(gap, "topic", "")
        if topic:
            lines.append(f"[gap] {topic}")
    if not lines:
        return None
    return "\n".join(lines)


def _build_distiller_env(config: AtelierConfig) -> dict[str, str]:
    """Synthesize the env mapping a :class:`DistillerConfig` expects.

    Atelier owns the database URL and the ``ATELIER_DISTILL_*`` knobs,
    but distill itself reads ``DATABASE_URL`` plus the ``DISTILL_*``
    prefix. We pass through any ``DISTILL_*`` env vars set on the
    running process so operators can keep tuning provider knobs without
    threading every flag through atelier's config.
    """

    if not config.distill_database_url:
        raise ConfigError("distill_database_url is required to build DistillMemoryService")
    env: dict[str, str] = {"DATABASE_URL": config.distill_database_url}
    for key in (
        "DISTILL_LLM_PROVIDER",
        "DISTILL_LLM_MODEL",
        "DISTILL_LLM_API_KEY",
        "DISTILL_LLM_BASE_URL",
        "DISTILL_EMBEDDING_PROVIDER",
        "DISTILL_EMBEDDING_MODEL",
        "DISTILL_EMBEDDING_DIM",
        "DISTILL_EMBEDDING_API_KEY",
    ):
        value = os.environ.get(key)
        if value:
            env[key] = value
    return env


class DistillMemoryService:
    """:class:`MemoryService` that delegates to stratoclave-distill.

    Holds one asyncpg pool, one :class:`Distiller`, one :class:`Curator`,
    and one :class:`Retriever` for the lifetime of the atelier server.
    Cleanup is via :meth:`aclose`, called by the FastAPI lifespan.
    """

    enabled = True

    def __init__(
        self,
        *,
        pool: Any,
        ingest_runner: Any,
        retriever: Any,
        version_id: str,
    ) -> None:
        self._pool = pool
        self._ingest_runner = ingest_runner
        self._retriever = retriever
        self._version_id = version_id
        self._closed = False

    @classmethod
    async def from_config(cls, config: AtelierConfig) -> DistillMemoryService:
        """Open the asyncpg pool and wire the ingest / retrieve pipelines.

        Coroutine because asyncpg's pool open is itself a coroutine; the
        FastAPI lifespan callback awaits this once at startup so the
        cost is amortised.
        """

        if not config.distill_enabled:
            raise ConfigError("DistillMemoryService requires distill_enabled=True")

        from stratoclave_distill.config import DistillerConfig
        from stratoclave_distill.db.asyncpg import (
            AsyncpgDigestStore,
            AsyncpgLearningStore,
            AsyncpgPurposeStore,
            AsyncpgWatermarkStore,
            open_pool,
        )
        from stratoclave_distill.pipeline import (
            Curator,
            Distiller,
            IngestRunner,
        )
        from stratoclave_distill.providers.embedding import (
            build_embedding_provider,
        )
        from stratoclave_distill.providers.llm import build_llm_provider
        from stratoclave_distill.retrieval.retriever import Retriever

        cfg = DistillerConfig.from_env(_build_distiller_env(config))
        version_id = "atelier-memory"

        pool = await open_pool(cfg.database_url)
        try:
            llm = build_llm_provider(cfg)
            embedder = build_embedding_provider(cfg)
            learnings = AsyncpgLearningStore(pool)
            curator = Curator(
                learnings,
                tau_merge=cfg.tau_merge,
                tau_conflict=cfg.tau_conflict,
                rrf_k=cfg.rrf_k,
            )
            distiller = Distiller(llm, embedder, version_id=version_id)
            ingest_runner = IngestRunner(
                distiller=distiller,
                curator=curator,
                watermarks=AsyncpgWatermarkStore(pool),
                purposes=AsyncpgPurposeStore(pool),
                digests=AsyncpgDigestStore(pool),
            )
            retriever = Retriever(learnings, embedder)
        except Exception:
            await pool.close()
            raise

        return cls(
            pool=pool,
            ingest_runner=ingest_runner,
            retriever=retriever,
            version_id=version_id,
        )

    async def ingest_session(
        self,
        *,
        session_id: UUID,
        events: Sequence[Event],
    ) -> None:
        """Convert turn events into NormalizedTurns and run the distill pipeline."""

        if self._closed:
            return
        turns = [_event_to_normalized_turn(event) for event in events if event.kind == "turn"]
        if not turns:
            return
        try:
            report = await self._ingest_runner.run_turns(turns)
        except Exception:
            logger.exception("distill ingest failed for session %s", session_id)
            return
        if report.error_count:
            logger.warning(
                "distill ingest reported %d errors for session %s",
                report.error_count,
                session_id,
            )

    async def retrieve(self, *, query: str, top_k: int = 5) -> str | None:
        """Embed the prompt and return canonical / emerging hits as a block."""

        if self._closed or not query.strip():
            return None
        try:
            result = await self._retriever.retrieve(query)
        except Exception:
            logger.exception("distill retrieve failed for query (len=%d)", len(query))
            return None
        return _format_retrieval(result)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._pool.close()
        except Exception:
            logger.exception("error closing distill connection pool")


__all__ = ["DistillMemoryService"]
