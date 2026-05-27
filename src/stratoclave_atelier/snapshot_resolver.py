"""Cross-session snapshot query resolver.

Stage D introduces a single new RPC: "given a frozen :class:`Version`,
answer this question". The actual resolver is pluggable: Stage D shipped
:class:`EchoSnapshotResolver` (a deterministic stub) so the audit log
records something useful without a live LLM. Stage I adds
:class:`DistillSnapshotResolver`, which:

* reads the version's JSONL bytes back via the :class:`BlobStore`,
* embeds the question into the same distill ``Retriever`` that powers
  the in-flight ``<memory>`` lane, and
* renders both into a single deterministic string -- no LLM call --
  so the answer is reproducible and unit-testable.

We deliberately keep the resolver behind a Protocol rather than calling
the LLM inline from the API handler:

* the same handler is used by tests (which need determinism) and the
  walking skeleton (which has no LLM credentials);
* the resolver has access to both the :class:`Store` and the
  :class:`BlobStore`, so it can fetch the JSONL bytes of the target
  version without the handler caring how that data is shaped.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from typing import Any, Protocol

from stratoclave_atelier.blobs import BlobStore
from stratoclave_atelier.config import AtelierConfig
from stratoclave_atelier.core import Version
from stratoclave_atelier.core.errors import ConfigError
from stratoclave_atelier.db import Store

logger = logging.getLogger(__name__)


class SnapshotResolver(Protocol):
    """Resolve a snapshot query against a frozen Version.

    Implementations may consult the :class:`Store` (e.g. fetch the
    Version row's metadata) and the :class:`BlobStore` (to read the
    canonical JSONL bytes); they MUST return a deterministic string for
    a given input or persist their non-determinism elsewhere.
    """

    async def resolve(
        self,
        *,
        store: Store,
        blob_store: BlobStore,
        version: Version,
        query: str,
    ) -> str: ...


class EchoSnapshotResolver(SnapshotResolver):
    """Reference resolver that echoes the query.

    The response includes the version's label (when set) and turn count
    so a UI rendering the audit row has something more interesting to
    show than the raw query. Used by unit tests and the walking
    skeleton; production deployments swap in an LLM-backed resolver.
    """

    async def resolve(
        self,
        *,
        store: Store,
        blob_store: BlobStore,
        version: Version,
        query: str,
    ) -> str:
        label = version.label or "<unlabeled>"
        return (
            f"[echo] version={label} turns={version.turn_count} "
            f"start_seq={version.start_seq} end_seq={version.end_seq} :: {query}"
        )


def _summarize_turn_roles(payload: bytes, *, max_lines: int = 200) -> str:
    """Render a one-line role histogram from a JSONL payload.

    The freeze pipeline writes one normalized turn per line; here we only
    care about the ``role`` field for the summary. We cap the number of
    parsed lines so an unusually large blob does not blow the resolver's
    latency budget.
    """

    if not payload:
        return "<empty>"
    counter: Counter[str] = Counter()
    for raw in payload.splitlines()[:max_lines]:
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        role = str(row.get("role") or "<unknown>")
        counter[role] += 1
    if not counter:
        return "<no-turns>"
    parts = [f"{role}={count}" for role, count in sorted(counter.items())]
    return ", ".join(parts)


def _format_retrieval(result: Any) -> str | None:
    """Render a distill ``RetrievalResult`` as a multi-line block.

    Mirrors the formatter in :mod:`stratoclave_atelier._distill_memory`
    so the snapshot answer reads the same as the in-flight memory lane.
    Returns ``None`` when there is nothing to surface.
    """

    lines: list[str] = []
    for hit in getattr(result, "canonical", ()):
        rule = getattr(getattr(hit, "learning", None), "rule", "")
        if rule:
            lines.append(f"[canonical] {rule}")
    for hit in getattr(result, "emerging", ()):
        rule = getattr(getattr(hit, "learning", None), "rule", "")
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


class DistillSnapshotResolver(SnapshotResolver):
    """Resolver that joins the version JSONL with distill retrieval hits.

    The answer string is composed from three deterministic sections:

    * ``[distill] version=... turns=...`` header echoing the version
      metadata so the audit row carries enough breadcrumbs even without
      live retrieval.
    * ``query: <question>`` for symmetry with EchoSnapshotResolver.
    * ``turn-roles: user=N, assistant=M`` so the operator can see at a
      glance what the version contains without re-reading the JSONL.
    * ``memory:\\n[canonical] ...`` when the distill ``Retriever`` finds
      learnings relevant to the question. Empty retrieval is rendered as
      ``memory: <none>`` rather than dropped, so callers can tell "no
      memory hits" apart from "memory disabled".

    The resolver owns its own asyncpg connection pool because Stage I
    keeps the snapshot path independent of the live ``MemoryService`` --
    a future stage can collapse them into one pool if the operational
    cost is significant.
    """

    def __init__(self, *, pool: Any, retriever: Any) -> None:
        self._pool = pool
        self._retriever = retriever
        self._closed = False

    @classmethod
    async def from_config(cls, config: AtelierConfig) -> DistillSnapshotResolver:
        """Open the asyncpg pool and wire the distill retriever.

        Coroutine because ``open_pool`` is itself async; the FastAPI
        lifespan callback awaits this once at startup and pairs it with
        :meth:`aclose` on shutdown.
        """

        if not config.distill_enabled:
            raise ConfigError("DistillSnapshotResolver requires distill_enabled=True")

        from stratoclave_distill.config import DistillerConfig
        from stratoclave_distill.db.asyncpg import (
            AsyncpgLearningStore,
            open_pool,
        )
        from stratoclave_distill.providers.embedding import (
            build_embedding_provider,
        )
        from stratoclave_distill.retrieval.retriever import Retriever

        from stratoclave_atelier._distill_memory import _build_distiller_env

        cfg = DistillerConfig.from_env(_build_distiller_env(config))
        pool = await open_pool(cfg.database_url)
        try:
            embedder = build_embedding_provider(cfg)
            learnings = AsyncpgLearningStore(pool)
            retriever = Retriever(learnings, embedder)
        except Exception:
            await pool.close()
            raise
        return cls(pool=pool, retriever=retriever)

    async def resolve(
        self,
        *,
        store: Store,
        blob_store: BlobStore,
        version: Version,
        query: str,
    ) -> str:
        label = version.label or "<unlabeled>"
        header = (
            f"[distill] version={label} turns={version.turn_count} "
            f"start_seq={version.start_seq} end_seq={version.end_seq}"
        )

        try:
            payload = await blob_store.read(version.blob_sha)
        except FileNotFoundError:
            payload = b""
        except Exception:  # pragma: no cover - defensive
            logger.exception("snapshot resolver could not read blob %s", version.blob_sha)
            payload = b""
        roles_summary = _summarize_turn_roles(payload)

        memory_block: str | None = None
        if query.strip():
            try:
                result = await self._retriever.retrieve(query)
                memory_block = _format_retrieval(result)
            except Exception:
                logger.exception("snapshot resolver retrieve failed for query (len=%d)", len(query))
                memory_block = None

        sections = [header, f"query: {query}", f"turn-roles: {roles_summary}"]
        if memory_block is None:
            sections.append("memory: <none>")
        else:
            sections.append("memory:\n" + memory_block)
        return "\n".join(sections)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._pool.close()
        except Exception:  # pragma: no cover - defensive
            logger.exception("error closing distill snapshot resolver pool")
