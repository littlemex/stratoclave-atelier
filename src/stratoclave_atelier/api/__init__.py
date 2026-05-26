"""API layer for stratoclave-atelier.

Stage B exposed ``health``, ``groups``, and ``sessions``. Stage C adds
``ingest`` (WebSocket) and ``events`` (SSE replay); freeze landed on
the existing ``sessions`` router.
"""

from stratoclave_atelier.api.events import router as events_router
from stratoclave_atelier.api.groups import router as groups_router
from stratoclave_atelier.api.health import router as health_router
from stratoclave_atelier.api.ingest import router as ingest_router
from stratoclave_atelier.api.sessions import router as sessions_router

__all__ = [
    "events_router",
    "groups_router",
    "health_router",
    "ingest_router",
    "sessions_router",
]
