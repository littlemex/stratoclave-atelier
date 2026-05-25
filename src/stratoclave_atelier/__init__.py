"""stratoclave-atelier: workshop for agent sessions.

Public surface area kept intentionally small. Internal modules
(:mod:`stratoclave_atelier.db`, :mod:`stratoclave_atelier.api`) are
importable but their stability is not guaranteed before v0.1 ships.
"""

from stratoclave_atelier.config import AtelierAuthMode, AtelierConfig
from stratoclave_atelier.core import (
    AtelierError,
    ConfigError,
    Group,
    NotFoundError,
    SchemaError,
    Session,
    SessionStatus,
    Version,
)

__all__ = [
    "AtelierAuthMode",
    "AtelierConfig",
    "AtelierError",
    "ConfigError",
    "Group",
    "NotFoundError",
    "SchemaError",
    "Session",
    "SessionStatus",
    "Version",
]

__version__ = "0.1.0.dev0"
