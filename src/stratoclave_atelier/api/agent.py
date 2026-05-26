"""``GET /api/agent/backends`` -- discover loom backends available on this server.

Stage H lets the chat surface pick a backend per session. The picker
needs to know:

* which backends the operator has greenlit
  (``ATELIER_AGENT_BACKENDS_ALLOWED``);
* which one is the default (used when the picker is left untouched);
* whether each backend has a usable ``cwd`` configured (so we can grey
  out picks that would 503 anyway).

The endpoint is intentionally read-only and returns 200 with an empty
list when no backends are configured -- the SPA renders that as "agent
backend disabled" instead of treating it as an error.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from stratoclave_atelier.api.deps import ConfigDep

router = APIRouter(prefix="/api/agent", tags=["agent"])


class BackendInfo(BaseModel):
    """One backend entry returned by :func:`list_backends`."""

    name: str = Field(..., description="Backend identifier (claude_code / kiro_code / mock).")
    ready: bool = Field(
        ..., description="True when the backend has a usable cwd configured on this server."
    )
    cwd: str | None = Field(
        default=None, description="The cwd the backend will launch with (None when unconfigured)."
    )


class BackendList(BaseModel):
    backends: list[BackendInfo]
    default: str | None = Field(
        default=None,
        description=(
            "Backend used when the session does not specify one. "
            "None when the server has no agent backend enabled."
        ),
    )


@router.get("/backends", response_model=BackendList)
async def list_backends(config: ConfigDep) -> BackendList:
    allowed = config.resolved_backends()
    default: str | None = None if config.agent_backend == "none" else config.agent_backend
    if default is None and len(allowed) == 1:
        # Stage G back-compat: a single allowed backend is the default
        # even when ATELIER_AGENT_BACKEND is left at its 'none' default.
        default = allowed[0]
    backends = [
        BackendInfo(
            name=name,
            cwd=config.cwd_for_backend(name),
            ready=bool(config.cwd_for_backend(name)),
        )
        for name in allowed
    ]
    return BackendList(backends=backends, default=default)
