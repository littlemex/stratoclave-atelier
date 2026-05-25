"""API layer for stratoclave-atelier.

Stage A only ships the health endpoint. Stage B onwards will add:

- ``groups``  -- group CRUD
- ``sessions`` -- session create / fork / list, JSONL ingest, event SSE
- ``versions`` -- version freeze (full + turn-range), download, listing
- ``fork_graph`` -- DAG JSON for the UI
"""

from stratoclave_atelier.api.health import router as health_router

__all__ = ["health_router"]
