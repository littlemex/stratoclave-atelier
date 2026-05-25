"""Dependency-injection helpers for the FastAPI app.

The :class:`Store` is attached to ``app.state.store`` by
:func:`stratoclave_atelier.server.create_app` (or by tests that build an
app around an :class:`InMemoryStore`). Route handlers use
:func:`get_store` so the handler signature stays free of FastAPI globals
and remains independently testable.
"""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import Depends, HTTPException, Request, status

from stratoclave_atelier.core import ConflictError, NotFoundError
from stratoclave_atelier.db import Store


def get_store(request: Request) -> Store:
    """Return the :class:`Store` attached to the FastAPI app."""

    store = getattr(request.app.state, "store", None)
    if store is None:  # pragma: no cover -- developer error if hit
        raise RuntimeError(
            "Store is not configured on app.state.store; "
            "build the app via create_app() with a Store"
        )
    return cast(Store, store)


StoreDep = Annotated[Store, Depends(get_store)]


def http_not_found(error: NotFoundError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error))


def http_conflict(error: ConflictError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))
