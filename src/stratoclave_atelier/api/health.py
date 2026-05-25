"""Liveness endpoint.

Returns 200 with a static body. Used as a Kubernetes / docker-compose
healthcheck and as the smoke test in CI for Stage A.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Return a static OK payload.

    Stage A's only working endpoint. Once the asyncpg pool is wired in
    (Stage B), this should also verify the DB connection on demand.
    """

    return {"status": "ok"}
