"""FastAPI app factory for stratoclave-atelier.

The module-level ``app`` is what uvicorn imports; ``create_app()`` is the
factory that tests and the CLI call when they want to inject a custom
:class:`AtelierConfig`. Stage A only mounts ``/healthz``; subsequent
stages will register the real routers (groups / sessions / versions /
fork_graph) here.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from fastapi import FastAPI

from stratoclave_atelier import __version__
from stratoclave_atelier.api import health_router
from stratoclave_atelier.config import AtelierConfig


def create_app(config: AtelierConfig | None = None) -> FastAPI:
    """Build a FastAPI application bound to the given config.

    If ``config`` is ``None`` we fall back to ``AtelierConfig.from_env()``
    -- which is what the module-level ``app`` does so uvicorn can import
    a ready-to-serve instance with a single env var
    (``ATELIER_DATABASE_URL``).
    """

    cfg = config or AtelierConfig.from_env()
    app = FastAPI(
        title="stratoclave-atelier",
        version=__version__,
        description=("Workshop for agent sessions: fork, freeze, and group conversations."),
    )
    app.state.config = cfg
    app.include_router(health_router)
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
