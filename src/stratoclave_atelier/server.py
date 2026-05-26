"""FastAPI app factory for stratoclave-atelier.

The module-level ``app`` is what uvicorn imports; ``create_app()`` is the
factory that tests and the CLI call when they want to inject a custom
:class:`AtelierConfig` and/or a custom :class:`Store`.

Stage B mounts ``health``, ``groups``, and ``sessions`` routers and
wires the asyncpg-backed :class:`Store` to ``app.state.store`` via
FastAPI lifespan callbacks. Tests can pass an ``InMemoryStore`` to
:func:`create_app` to avoid needing a database.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager

from fastapi import FastAPI

from stratoclave_atelier import __version__
from stratoclave_atelier.api import groups_router, health_router, sessions_router
from stratoclave_atelier.config import AtelierConfig
from stratoclave_atelier.db import AsyncpgStore, Store, create_engine


def create_app(
    config: AtelierConfig | None = None,
    *,
    store: Store | None = None,
) -> FastAPI:
    """Build a FastAPI application bound to the given config and store.

    If ``config`` is ``None`` we fall back to ``AtelierConfig.from_env()``
    -- which is what the module-level ``app`` does so uvicorn can import
    a ready-to-serve instance with a single env var
    (``ATELIER_DATABASE_URL``).

    If ``store`` is ``None`` an :class:`AsyncpgStore` is built from the
    config inside the lifespan; tests that want to inject an
    :class:`InMemoryStore` should pass it explicitly.
    """

    cfg = config or AtelierConfig.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if store is not None:
            app.state.store = store
            yield
            return
        engine = create_engine(cfg.database_url)
        runtime_store = AsyncpgStore(engine)
        app.state.store = runtime_store
        try:
            yield
        finally:
            await runtime_store.dispose()

    app = FastAPI(
        title="stratoclave-atelier",
        version=__version__,
        description=("Workshop for agent sessions: fork, freeze, and group conversations."),
        lifespan=lifespan,
    )
    app.state.config = cfg
    app.include_router(health_router)
    app.include_router(groups_router)
    app.include_router(sessions_router)
    return app


def _build_module_app() -> FastAPI:
    """Lazily build the app for ``uvicorn stratoclave_atelier.server:app``.

    During unit testing the env var may legitimately be unset; in that
    case we fall back to a placeholder URL so importing the module does
    not crash. Tests that exercise the real surface should call
    :func:`create_app` directly with their own config.
    """

    if "ATELIER_DATABASE_URL" not in os.environ:
        env: Mapping[str, str] = {
            "ATELIER_DATABASE_URL": "postgresql+asyncpg://atelier:atelier@localhost:5432/atelier",
        }
        return create_app(AtelierConfig.from_env(env))
    return create_app()


app = _build_module_app()
