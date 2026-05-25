"""Error hierarchy for stratoclave-atelier.

The library raises subclasses of :class:`AtelierError` so that callers
can catch a single base type if they only want broad-strokes handling,
but each subclass also carries enough information to drive targeted
recovery.
"""

from __future__ import annotations


class AtelierError(Exception):
    """Base class for all stratoclave-atelier errors."""


class ConfigError(AtelierError):
    """Raised when the runtime configuration is invalid or incomplete."""


class SchemaError(AtelierError):
    """Raised when the database schema is missing or out of sync."""


class NotFoundError(AtelierError):
    """Raised when a requested entity (group / session / version) is absent."""
