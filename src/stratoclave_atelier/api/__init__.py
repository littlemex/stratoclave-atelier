"""API layer for stratoclave-atelier.

Stage B exposed ``health``, ``groups``, and ``sessions``. Stage C adds
``ingest`` (WebSocket) and ``events`` (SSE replay); freeze landed on
the existing ``sessions`` router. Stage D adds ``fork_graph`` and
``snapshot_queries``.
"""

from stratoclave_atelier.api.events import router as events_router
from stratoclave_atelier.api.fork_graph import router as fork_graph_router
from stratoclave_atelier.api.groups import router as groups_router
from stratoclave_atelier.api.health import router as health_router
from stratoclave_atelier.api.ingest import router as ingest_router
from stratoclave_atelier.api.sessions import router as sessions_router
from stratoclave_atelier.api.snapshot_queries import router as snapshot_queries_router

__all__ = [
    "events_router",
    "fork_graph_router",
    "groups_router",
    "health_router",
    "ingest_router",
    "sessions_router",
    "snapshot_queries_router",
]
