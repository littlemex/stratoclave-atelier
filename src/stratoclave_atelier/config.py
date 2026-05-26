"""Runtime configuration for stratoclave-atelier.

Configuration is sourced in this priority order, highest first:

1. Explicit kwargs passed to :class:`AtelierConfig`.
2. Environment variables (``ATELIER_*``).
3. Library defaults defined in this module.

Hard-coded paths, URLs, and credentials are deliberately absent: every
deployment-specific setting must come from configuration. See
``docs/PROJECT_RULES.md`` for the no-hardcode policy.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from typing import Literal, cast

from stratoclave_atelier.core.errors import ConfigError

AtelierAuthMode = Literal["none", "bearer", "stratoclave_cognito"]
AtelierAgentBackend = Literal["none", "claude_code", "kiro_code", "mock"]
AtelierSnapshotResolver = Literal["echo", "distill"]

_DEFAULT_HOST = "0.0.0.0"
_DEFAULT_PORT = 8000
_DEFAULT_LOG_LEVEL = "info"
_DEFAULT_AUTH_MODE: AtelierAuthMode = "none"
_DEFAULT_BLOB_DIR = ".atelier-blobs"
_DEFAULT_AGENT_BACKEND: AtelierAgentBackend = "none"
_DEFAULT_SNAPSHOT_RESOLVER: AtelierSnapshotResolver = "echo"


def _read_int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from exc


@dataclass(frozen=True, slots=True)
class AtelierConfig:
    """Frozen runtime configuration for the atelier service.

    The defaults are chosen so that calling ``AtelierConfig.from_env({})``
    with the bare-minimum environment (``ATELIER_DATABASE_URL``) yields a
    working configuration. Anything fancier can be overridden via kwargs
    or env vars.
    """

    database_url: str
    host: str = _DEFAULT_HOST
    port: int = _DEFAULT_PORT
    log_level: str = _DEFAULT_LOG_LEVEL
    auth_mode: AtelierAuthMode = _DEFAULT_AUTH_MODE
    bearer_token: str | None = None
    blob_dir: str = _DEFAULT_BLOB_DIR
    agent_backend: AtelierAgentBackend = _DEFAULT_AGENT_BACKEND
    agent_cwd: str | None = None
    agent_allowed_tools: tuple[str, ...] = ()
    distill_enabled: bool = False
    distill_database_url: str | None = None
    distill_auto_ingest: bool = True
    agent_memory_enabled: bool = True
    snapshot_resolver: AtelierSnapshotResolver = _DEFAULT_SNAPSHOT_RESOLVER
    extra: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.database_url:
            raise ConfigError("database_url must be a non-empty string")
        if self.port < 1 or self.port > 65535:
            raise ConfigError(f"port must be in 1..65535, got {self.port}")
        if self.auth_mode == "bearer" and not self.bearer_token:
            raise ConfigError(
                "bearer_token is required when auth_mode='bearer' (set ATELIER_BEARER_TOKEN)"
            )
        if self.agent_backend != "none" and not self.agent_cwd:
            raise ConfigError(
                f"agent_cwd is required when agent_backend={self.agent_backend!r} "
                "(set ATELIER_AGENT_CWD)"
            )
        if self.distill_enabled and not self.distill_database_url:
            raise ConfigError(
                "distill_database_url is required when distill_enabled=True "
                "(set ATELIER_DISTILL_DATABASE_URL)"
            )
        if self.snapshot_resolver == "distill" and not self.distill_enabled:
            raise ConfigError(
                "snapshot_resolver='distill' requires distill_enabled=True "
                "(set ATELIER_DISTILL_ENABLED=true)"
            )

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        **overrides: object,
    ) -> AtelierConfig:
        """Build a config from a mapping (defaults to ``os.environ``).

        Explicit ``overrides`` take precedence over the env mapping. This
        is the entrypoint that the CLI and tests use; calling code should
        never read ``os.environ`` directly to keep configuration auditable.
        """

        src: Mapping[str, str] = os.environ if env is None else env

        def pop_str(key: str, env_key: str, default: str = "") -> str:
            if key in overrides:
                value = overrides.pop(key)
                return "" if value is None else str(value)
            return src.get(env_key, default)

        def pop_optional_str(key: str, env_key: str) -> str | None:
            if key in overrides:
                value = overrides.pop(key)
                if value is None:
                    return None
                text = str(value)
                return text or None
            raw = src.get(env_key)
            return raw or None

        def pop_int(key: str, env_key: str, default: int) -> int:
            if key in overrides:
                value = overrides.pop(key)
                return int(cast(int, value))
            return _read_int(src, env_key, default)

        def pop_bool(key: str, env_key: str, default: bool) -> bool:
            if key in overrides:
                value = overrides.pop(key)
                if isinstance(value, bool):
                    return value
                return str(value).lower() in ("1", "true", "yes", "on")
            raw = src.get(env_key)
            if raw is None or raw == "":
                return default
            return raw.lower() in ("1", "true", "yes", "on")

        def pop_csv(key: str, env_key: str) -> tuple[str, ...]:
            if key in overrides:
                value = overrides.pop(key)
                if value is None or value == "":
                    return ()
                if isinstance(value, str):
                    return tuple(s.strip() for s in value.split(",") if s.strip())
                return tuple(str(v) for v in cast(tuple[object, ...], value))
            raw = src.get(env_key, "")
            return tuple(s.strip() for s in raw.split(",") if s.strip())

        database_url = pop_str("database_url", "ATELIER_DATABASE_URL")
        if not database_url:
            raise ConfigError("ATELIER_DATABASE_URL is required")

        auth_mode = pop_str("auth_mode", "ATELIER_AUTH_MODE", _DEFAULT_AUTH_MODE)
        if auth_mode not in ("none", "bearer", "stratoclave_cognito"):
            raise ConfigError(
                f"unsupported auth_mode {auth_mode!r}; "
                "expected one of: none, bearer, stratoclave_cognito"
            )

        agent_backend = pop_str("agent_backend", "ATELIER_AGENT_BACKEND", _DEFAULT_AGENT_BACKEND)
        if agent_backend not in ("none", "claude_code", "kiro_code", "mock"):
            raise ConfigError(
                f"unsupported agent_backend {agent_backend!r}; "
                "expected one of: none, claude_code, kiro_code, mock"
            )

        snapshot_resolver = pop_str(
            "snapshot_resolver", "ATELIER_SNAPSHOT_RESOLVER", _DEFAULT_SNAPSHOT_RESOLVER
        )
        if snapshot_resolver not in ("echo", "distill"):
            raise ConfigError(
                f"unsupported snapshot_resolver {snapshot_resolver!r}; "
                "expected one of: echo, distill"
            )

        cfg = cls(
            database_url=database_url,
            host=pop_str("host", "ATELIER_HOST", _DEFAULT_HOST),
            port=pop_int("port", "ATELIER_PORT", _DEFAULT_PORT),
            log_level=pop_str("log_level", "ATELIER_LOG_LEVEL", _DEFAULT_LOG_LEVEL),
            auth_mode=cast(AtelierAuthMode, auth_mode),
            bearer_token=pop_optional_str("bearer_token", "ATELIER_BEARER_TOKEN"),
            blob_dir=pop_str("blob_dir", "ATELIER_BLOB_DIR", _DEFAULT_BLOB_DIR),
            agent_backend=cast(AtelierAgentBackend, agent_backend),
            agent_cwd=pop_optional_str("agent_cwd", "ATELIER_AGENT_CWD"),
            agent_allowed_tools=pop_csv("agent_allowed_tools", "ATELIER_AGENT_ALLOWED_TOOLS"),
            distill_enabled=pop_bool("distill_enabled", "ATELIER_DISTILL_ENABLED", False),
            distill_database_url=pop_optional_str(
                "distill_database_url", "ATELIER_DISTILL_DATABASE_URL"
            ),
            distill_auto_ingest=pop_bool(
                "distill_auto_ingest", "ATELIER_DISTILL_AUTO_INGEST", True
            ),
            agent_memory_enabled=pop_bool("agent_memory_enabled", "ATELIER_AGENT_MEMORY", True),
            snapshot_resolver=cast(AtelierSnapshotResolver, snapshot_resolver),
        )
        if overrides:
            unknown = ", ".join(sorted(overrides))
            raise ConfigError(f"unknown configuration overrides: {unknown}")
        return cfg

    def field_names(self) -> tuple[str, ...]:
        """Return all dataclass field names. Useful for diagnostics."""

        return tuple(f.name for f in fields(self))
