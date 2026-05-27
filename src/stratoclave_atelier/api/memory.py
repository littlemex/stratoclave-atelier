"""``POST /api/memory/query`` and ``/api/memory/adopt`` (Stage K).

Stage K's "ask another session" feature has two paths:

* the *primary* path delegates to :class:`MemoryService` -- effectively
  the distill retriever -- so the SPA can ask "what canonical /
  emerging facts match this query, scoped to these sessions?" and
  splice the result into a ``<memory>`` block before the next agent
  turn;
* the *fallback* path is per-session raw event search and lives on
  ``/api/sessions/{id}/events/search``; ``/query`` covers the primary
  path only.

After the user reviews the retrieved block they can *adopt* it via
``/api/memory/adopt``. That endpoint stashes the block on the
:class:`AgentRunner` keyed by atelier ``session_id``; the very next
``run()`` for that session pops the block and prepends it as a
``<memory>`` segment regardless of the server's auto-retrieval setting.
The state is intentionally in-process and short-lived: re-adopting
overwrites; explicit clears wipe; an agent run consumes; no
persistence across restarts.

The query handler is intentionally tolerant of the noop case: when
distill is disabled the response carries ``enabled=False`` and an
empty ``memory_block``; the SPA renders that as "memory disabled, fall
back to raw search" without erroring out.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from stratoclave_atelier.agent_runner import AgentRunner
from stratoclave_atelier.api.deps import MemoryServiceDep, StoreDep, http_not_found
from stratoclave_atelier.core import NotFoundError

router = APIRouter(prefix="/api/memory", tags=["memory"])


def _get_agent_runner(request: Request) -> AgentRunner:
    runner = getattr(request.app.state, "agent_runner", None)
    if runner is None:  # pragma: no cover -- developer error if hit
        raise RuntimeError("AgentRunner is not configured on app.state.agent_runner")
    return cast(AgentRunner, runner)


AgentRunnerDep = Annotated[AgentRunner, Depends(_get_agent_runner)]


class MemoryQueryRequest(BaseModel):
    """Input for ``POST /api/memory/query``.

    ``session_ids`` (when set) restricts retrieval to learnings whose
    ``source_session`` matches one of the provided atelier session ids.
    ``None`` means "all sessions"; an empty list means "no allowed
    sessions" and yields ``memory_block=None`` regardless of distill
    state.
    """

    query: str = Field(..., min_length=1, max_length=4_000)
    session_ids: list[UUID] | None = Field(
        default=None,
        description=(
            "Optional list of atelier session ids to scope the retrieval to. "
            "None means 'every session that has been distilled into memory'."
        ),
    )
    top_k: int = Field(default=5, ge=1, le=50)


class MemoryQueryResponse(BaseModel):
    enabled: bool = Field(
        ...,
        description=(
            "True when the underlying MemoryService actually reaches distill. "
            "False (NoopMemoryService) is the default when distill is disabled."
        ),
    )
    memory_block: str | None = Field(
        default=None,
        description=(
            "The retrieved memory rendered as a ``<memory>``-ready string, or "
            "``None`` when nothing matched / memory is disabled."
        ),
    )
    queried_session_ids: list[UUID] | None = Field(
        default=None,
        description="Echoes back the scope passed in, for the SPA's chip rendering.",
    )


@router.post("/query", response_model=MemoryQueryResponse)
async def query_memory(
    body: MemoryQueryRequest,
    memory: MemoryServiceDep,
) -> MemoryQueryResponse:
    """Run a scoped retrieval against the cross-session memory."""

    scope: Sequence[UUID] | None = body.session_ids
    block = await memory.retrieve(
        query=body.query,
        top_k=body.top_k,
        scope_session_ids=scope,
    )
    return MemoryQueryResponse(
        enabled=memory.enabled,
        memory_block=block,
        queried_session_ids=body.session_ids,
    )


class MemoryAdoptRequest(BaseModel):
    """Stash ``memory_block`` for the next agent run on ``session_id``."""

    session_id: UUID = Field(..., description="Atelier session id that will consume the block.")
    memory_block: str = Field(..., min_length=1, max_length=64_000)
    queried_session_ids: list[UUID] | None = Field(
        default=None,
        description=(
            "Optional record of which atelier sessions the block was retrieved "
            "from; persisted onto the next user-turn payload so freeze/replay "
            "reflects the cross-session reference."
        ),
    )


class MemoryAdoptResponse(BaseModel):
    session_id: UUID
    pending: bool = Field(
        ...,
        description=(
            "True when a memory block is queued for the session's next run. "
            "False when ``memory_block`` was explicitly cleared."
        ),
    )


class MemoryPendingResponse(BaseModel):
    session_id: UUID
    pending: bool
    memory_block: str | None = None


@router.post("/adopt", response_model=MemoryAdoptResponse, status_code=status.HTTP_202_ACCEPTED)
async def adopt_memory(
    body: MemoryAdoptRequest,
    store: StoreDep,
    runner: AgentRunnerDep,
) -> MemoryAdoptResponse:
    """Queue ``memory_block`` for injection on the next agent run.

    The session must exist and be active; adopting against a frozen or
    archived session is a 409 because the SPA's "next run" never
    happens for those.
    """

    try:
        session = await store.get_session(body.session_id)
    except NotFoundError as exc:
        raise http_not_found(exc) from exc
    if session.status != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"session {body.session_id} is {session.status}, cannot adopt memory",
        )
    runner.adopt_memory(body.session_id, body.memory_block)
    return MemoryAdoptResponse(session_id=body.session_id, pending=True)


@router.delete("/adopt/{session_id}", response_model=MemoryAdoptResponse)
async def clear_pending_memory(
    session_id: UUID,
    runner: AgentRunnerDep,
) -> MemoryAdoptResponse:
    """Drop any pending block without consuming it."""

    runner.clear_pending_memory(session_id)
    return MemoryAdoptResponse(session_id=session_id, pending=False)


@router.get("/adopt/{session_id}", response_model=MemoryPendingResponse)
async def peek_pending_memory(
    session_id: UUID,
    runner: AgentRunnerDep,
) -> MemoryPendingResponse:
    """Inspect the pending block (used by the SPA to render the chip)."""

    block = runner.peek_pending_memory(session_id)
    return MemoryPendingResponse(
        session_id=session_id,
        pending=block is not None,
        memory_block=block,
    )


__all__ = ["router"]
