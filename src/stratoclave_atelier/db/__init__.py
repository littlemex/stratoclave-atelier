"""Database layer for stratoclave-atelier.

Exposes the :class:`Store` Protocol and the in-memory implementation
used by tests. The asyncpg-backed implementation lives in
:mod:`stratoclave_atelier.db.asyncpg_store` and is imported lazily by
the API layer so test environments do not need an asyncpg-compatible
event loop.
"""

from stratoclave_atelier.db.asyncpg_store import AsyncpgStore, create_engine
from stratoclave_atelier.db.memory import InMemoryStore
from stratoclave_atelier.db.store import Store

__all__ = ["AsyncpgStore", "InMemoryStore", "Store", "create_engine"]
