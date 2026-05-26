"""``POST /api/sessions/{id}/agent-runs`` endpoint.

This is the chat HTTP entrypoint for Stage G. The handler kicks off a
loom-driven agent run on the server and returns ``202 Accepted`` as
soon as the background task is scheduled. Clients listen on
``GET /api/sessions/{id}/events`` (live SSE) for the streaming
response.

Why ``202`` + SSE instead of streaming the response inline:

* the SPA already maintains an :class:`EventSource` for live tail; one
  channel is simpler than two,
* freeze / replay must see the same events the SPA sees, so we always
  go through the ``events`` log,
* multiple SPA tabs on the same session share the same stream.
"""

from __future__ import annotations

from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from stratoclave_atelier.agent_runner import AgentRunner
from stratoclave_atelier.api.deps import StoreDep, http_conflict, http_not_found
from stratoclave_atelier.core import ConflictError, NotFoundError

router = APIRouter(prefix="/api/sessions", tags=["agent-runs"])


class AgentRunCreate(BaseModel):
    """Request body for ``POST /api/sessions/{id}/agent-runs``."""

    prompt: str = Field(min_length=1)
    memory: bool | None = Field(
        default=None, description="Override server default for memory inject"
    )


class AgentRunRead(BaseModel):
    session_id: UUID
    status: str = Field(default="scheduled")


def _get_agent_runner(request: Request) -> AgentRunner:
    runner = getattr(request.app.state, "agent_runner", None)
    if runner is None:  # pragma: no cover -- developer error if hit
        raise RuntimeError("AgentRunner is not configured on app.state.agent_runner")
    return cast(AgentRunner, runner)


AgentRunnerDep = Annotated[AgentRunner, Depends(_get_agent_runner)]


@router.post(
    "/{session_id}/agent-runs",
    response_model=AgentRunRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_agent_run(
    session_id: UUID,
    payload: AgentRunCreate,
    store: StoreDep,
    runner: AgentRunnerDep,
) -> AgentRunRead:
    if not runner.enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="agent backend disabled; set ATELIER_AGENT_BACKEND to enable",
        )
    try:
        session = await store.get_session(session_id)
    except NotFoundError as exc:
        raise http_not_found(exc) from exc
    if session.status != "active":
        raise http_conflict(ConflictError(f"session {session_id} is {session.status}, not active"))

    # Fire-and-forget: the SPA listens to the SSE stream for chunks. The
    # task is kept on the runner so the event loop holds a strong
    # reference and Python doesn't garbage-collect it mid-flight.
    runner.schedule(session_id=session_id, prompt=payload.prompt)
    return AgentRunRead(session_id=session_id)


@router.post(
    "/{session_id}/agent-runs/cancel",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def cancel_agent_run(
    session_id: UUID,
    runner: AgentRunnerDep,
) -> None:
    await runner.cancel(session_id)
