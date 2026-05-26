"""API layer for stratoclave-atelier.

Stage B exposes ``health``, ``groups``, and ``sessions`` routers.
Subsequent stages will add JSONL ingest, freeze RPC, fork-graph JSON,
and cross-session snapshot queries.
"""

from stratoclave_atelier.api.groups import router as groups_router
from stratoclave_atelier.api.health import router as health_router
from stratoclave_atelier.api.sessions import router as sessions_router

__all__ = ["groups_router", "health_router", "sessions_router"]
