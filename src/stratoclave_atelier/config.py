"""Runtime configuration for stratoclave-atelier.

Configuration is sourced in this priority order, highest first:

1. Explicit kwargs passed to :class:`AtelierConfig`.
2. Environment variables (``ATELIER_*``).
3. Library defaults defined in this module.

Hard-coded paths, URLs, and credentials are deliberately absent: every
deployment-specific setting must come from configuration. See
``docs/PROJECT_RULES.md`` for the no-hardcode policy.

Stage H introduces *per-session* backend selection: callers can pick
``claude_code`` / ``kiro_code`` / ``mock`` at session creation time. The
config therefore exposes both a *default* backend (``agent_backend``,
back-compat with Stage G's single-backend ``ATELIER_AGENT_BACKEND``) and
the list of *allowed* backends (``agent_backends_allowed``) along with
per-backend ``cwd`` and ``allowed_tools`` overrides.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from types import MappingProxyType
from typing import Literal, cast

from stratoclave_atelier.core.errors import ConfigError

AtelierAuthMode = Literal["none", "bearer", "stratoclave_cognito"]
AtelierAgentBackend = Literal["none", "claude_code", "kiro_code", "mock"]
AtelierSnapshotResolver = Literal["echo", "distill"]
AtelierAgentCwdIsolation = Literal["per_session", "shared"]

_VALID_BACKENDS: tuple[str, ...] = ("claude_code", "kiro_code", "mock")

_DEFAULT_HOST = "0.0.0.0"
_DEFAULT_PORT = 8000
_DEFAULT_LOG_LEVEL = "info"
_DEFAULT_AUTH_MODE: AtelierAuthMode = "none"
_DEFAULT_BLOB_DIR = ".atelier-blobs"
_DEFAULT_AGENT_BACKEND: AtelierAgentBackend = "none"
_DEFAULT_SNAPSHOT_RESOLVER: AtelierSnapshotResolver = "echo"
_DEFAULT_AGENT_CWD_ISOLATION: AtelierAgentCwdIsolation = "per_session"


def _read_int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from exc


def _read_per_backend_str(
    src: Mapping[str, str],
    overrides: dict[str, object],
    *,
    key: str,
    env_prefix: str,
) -> Mapping[str, str]:
    """Read ``ATELIER_AGENT_<X>_<BACKEND>`` style env vars into a dict.

    The override kwarg, if present, takes precedence and must be a
    mapping. Backend names are normalised to lowercase to match the
    Literal alias.
    """

    if key in overrides:
        value = overrides.pop(key)
        if value is None or value == {}:
            return MappingProxyType({})
        if not isinstance(value, Mapping):
            raise ConfigError(f"{key} override must be a mapping")
        out: dict[str, str] = {}
        for raw_k, raw_v in value.items():
            if raw_v is None or raw_v == "":
                continue
            out[str(raw_k).lower()] = str(raw_v)
        return MappingProxyType(out)
    out = {}
    for backend in _VALID_BACKENDS:
        env_key = f"{env_prefix}{backend.upper()}"
        raw = src.get(env_key)
        if raw:
            out[backend] = raw
    return MappingProxyType(out)


def _read_per_backend_csv(
    src: Mapping[str, str],
    overrides: dict[str, object],
    *,
    key: str,
    env_prefix: str,
) -> Mapping[str, tuple[str, ...]]:
    """Same as :func:`_read_per_backend_str` but values are CSV tuples."""

    if key in overrides:
        value = overrides.pop(key)
        if value is None or value == {}:
            return MappingProxyType({})
        if not isinstance(value, Mapping):
            raise ConfigError(f"{key} override must be a mapping")
        out_csv: dict[str, tuple[str, ...]] = {}
        for raw_k, raw_v in value.items():
            if raw_v is None or raw_v == "" or raw_v == ():
                continue
            if isinstance(raw_v, str):
                parts = tuple(s.strip() for s in raw_v.split(",") if s.strip())
            elif isinstance(raw_v, tuple | list):
                parts = tuple(str(p) for p in raw_v if str(p).strip())
            else:
                raise ConfigError(f"{key}[{raw_k!r}] must be str/tuple/list")
            if parts:
                out_csv[str(raw_k).lower()] = parts
        return MappingProxyType(out_csv)
    out_csv = {}
    for backend in _VALID_BACKENDS:
        env_key = f"{env_prefix}{backend.upper()}"
        raw = src.get(env_key, "")
        parts = tuple(s.strip() for s in raw.split(",") if s.strip())
        if parts:
            out_csv[backend] = parts
    return MappingProxyType(out_csv)


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
    """Default backend used when a session does not specify one. Kept for
    Stage G back-compat; ``agent_backends_allowed`` is the source of
    truth for the picker."""
    agent_backends_allowed: tuple[str, ...] = ()
    """Backends the operator has greenlit for this deployment. The chat
    UI lists this set, and ``Session.agent_backend`` must lie in it (or
    be ``None`` to fall through to ``agent_backend``)."""
    agent_cwd: str | None = None
    """Default ``cwd`` for backends that don't have a per-backend
    override. Required iff ``agent_backend != 'none'`` and no per-backend
    override exists."""
    agent_allowed_tools: tuple[str, ...] = ()
    """Default ``allowed_tools`` for backends that don't override."""
    agent_cwd_by_backend: Mapping[str, str] = field(default_factory=dict)
    """Per-backend ``cwd`` overrides (keys: backend names)."""
    agent_allowed_tools_by_backend: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    """Per-backend ``allowed_tools`` overrides."""
    agent_cwd_isolation: AtelierAgentCwdIsolation = _DEFAULT_AGENT_CWD_ISOLATION
    """How agent cwds are scoped across atelier sessions.

    ``per_session`` (default) gives each atelier session_id its own
    ``${agent_cwd}/sessions/${session_id}`` directory so that any
    state the backend persists alongside its cwd (Claude Code's
    auto-memory, ``.claude/projects/`` transcripts, project-local
    configuration, etc.) does not leak between siblings or between
    parent/child branches. ``shared`` reverts to the Stage G behaviour
    where every session points at the same configured cwd.
    """
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
        # Validate the allowed-list: each entry must be a known backend
        # and must have a usable cwd (either per-backend or default).
        for name in self.agent_backends_allowed:
            if name not in _VALID_BACKENDS:
                raise ConfigError(
                    f"unsupported backend in agent_backends_allowed: {name!r}; "
                    f"expected one of: {', '.join(_VALID_BACKENDS)}"
                )
            if not self.cwd_for_backend(name):
                raise ConfigError(
                    f"agent_cwd is required for backend {name!r} "
                    f"(set ATELIER_AGENT_CWD or ATELIER_AGENT_CWD_{name.upper()})"
                )
        # The fallback backend must be in the allowed list (unless
        # 'none', which simply disables the runner globally).
        if (
            self.agent_backend != "none"
            and self.agent_backends_allowed
            and self.agent_backend not in self.agent_backends_allowed
        ):
            raise ConfigError(
                f"agent_backend {self.agent_backend!r} must appear in "
                f"agent_backends_allowed={self.agent_backends_allowed!r} "
                "(set ATELIER_AGENT_BACKENDS_ALLOWED accordingly)"
            )
        # Stage G back-compat: when the singular agent_backend is set
        # and the allowed list is empty, the default itself becomes the
        # only allowed backend; we still need a cwd.
        if (
            self.agent_backend != "none"
            and not self.agent_backends_allowed
            and not self.cwd_for_backend(self.agent_backend)
        ):
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

    # -- Stage H helpers --------------------------------------------------
    def cwd_for_backend(self, backend: str) -> str | None:
        """Return the ``cwd`` to use when launching ``backend``.

        Falls back to :attr:`agent_cwd` when no per-backend override is
        configured. ``None`` means "no cwd configured" -- the caller
        should treat this as a 503 condition for that backend.
        """

        return self.agent_cwd_by_backend.get(backend) or self.agent_cwd

    def allowed_tools_for_backend(self, backend: str) -> tuple[str, ...]:
        """Return the ``allowed_tools`` to use when launching ``backend``."""

        override = self.agent_allowed_tools_by_backend.get(backend)
        if override:
            return override
        return self.agent_allowed_tools

    def resolved_backends(self) -> tuple[str, ...]:
        """Return the effective list of allowed backends.

        Stage G back-compat: when the operator only set the singular
        ``ATELIER_AGENT_BACKEND``, that single value is the allowed list.
        ``"none"`` collapses to an empty tuple so the API can advertise
        "no backends".
        """

        if self.agent_backends_allowed:
            return self.agent_backends_allowed
        if self.agent_backend == "none":
            return ()
        return (self.agent_backend,)

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

        agent_cwd_isolation = pop_str(
            "agent_cwd_isolation",
            "ATELIER_AGENT_CWD_ISOLATION",
            _DEFAULT_AGENT_CWD_ISOLATION,
        )
        if agent_cwd_isolation not in ("per_session", "shared"):
            raise ConfigError(
                f"unsupported agent_cwd_isolation {agent_cwd_isolation!r}; "
                "expected one of: per_session, shared"
            )

        agent_backends_allowed = pop_csv("agent_backends_allowed", "ATELIER_AGENT_BACKENDS_ALLOWED")
        # Per-backend cwd / allowed_tools maps. We accept either kwarg
        # (``agent_cwd_by_backend={'kiro_code': '/wk'}``) or env vars
        # named ``ATELIER_AGENT_CWD_<BACKEND>`` (uppercased).
        agent_cwd_by_backend = _read_per_backend_str(
            src, overrides, key="agent_cwd_by_backend", env_prefix="ATELIER_AGENT_CWD_"
        )
        agent_allowed_tools_by_backend = _read_per_backend_csv(
            src,
            overrides,
            key="agent_allowed_tools_by_backend",
            env_prefix="ATELIER_AGENT_ALLOWED_TOOLS_",
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
            agent_backends_allowed=agent_backends_allowed,
            agent_cwd=pop_optional_str("agent_cwd", "ATELIER_AGENT_CWD"),
            agent_allowed_tools=pop_csv("agent_allowed_tools", "ATELIER_AGENT_ALLOWED_TOOLS"),
            agent_cwd_by_backend=agent_cwd_by_backend,
            agent_allowed_tools_by_backend=agent_allowed_tools_by_backend,
            distill_enabled=pop_bool("distill_enabled", "ATELIER_DISTILL_ENABLED", False),
            distill_database_url=pop_optional_str(
                "distill_database_url", "ATELIER_DISTILL_DATABASE_URL"
            ),
            distill_auto_ingest=pop_bool(
                "distill_auto_ingest", "ATELIER_DISTILL_AUTO_INGEST", True
            ),
            agent_memory_enabled=pop_bool("agent_memory_enabled", "ATELIER_AGENT_MEMORY", True),
            snapshot_resolver=cast(AtelierSnapshotResolver, snapshot_resolver),
            agent_cwd_isolation=cast(AtelierAgentCwdIsolation, agent_cwd_isolation),
        )
        if overrides:
            unknown = ", ".join(sorted(overrides))
            raise ConfigError(f"unknown configuration overrides: {unknown}")
        return cfg

    def field_names(self) -> tuple[str, ...]:
        """Return all dataclass field names. Useful for diagnostics."""

        return tuple(f.name for f in fields(self))
