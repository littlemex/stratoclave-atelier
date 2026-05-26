"""Tests for :mod:`stratoclave_atelier.config`."""

from __future__ import annotations

import pytest

from stratoclave_atelier.config import AtelierConfig
from stratoclave_atelier.core.errors import ConfigError


def test_from_env_minimal_database_url() -> None:
    cfg = AtelierConfig.from_env(
        {"ATELIER_DATABASE_URL": "postgresql+asyncpg://x:y@localhost:5432/z"}
    )
    assert cfg.database_url.endswith("/z")
    assert cfg.host == "0.0.0.0"
    assert cfg.port == 8000
    assert cfg.auth_mode == "none"
    assert cfg.bearer_token is None


def test_from_env_missing_database_url_raises() -> None:
    with pytest.raises(ConfigError, match="ATELIER_DATABASE_URL is required"):
        AtelierConfig.from_env({})


def test_from_env_overrides_take_precedence() -> None:
    cfg = AtelierConfig.from_env(
        {
            "ATELIER_DATABASE_URL": "postgresql+asyncpg://a:b@localhost:5432/c",
            "ATELIER_PORT": "8100",
        },
        port=9000,
    )
    assert cfg.port == 9000


def test_invalid_port_rejected() -> None:
    with pytest.raises(ConfigError, match=r"port must be in 1\.\.65535"):
        AtelierConfig.from_env(
            {
                "ATELIER_DATABASE_URL": "postgresql+asyncpg://a:b@localhost:5432/c",
                "ATELIER_PORT": "0",
            }
        )


def test_non_integer_port_raises() -> None:
    with pytest.raises(ConfigError, match="ATELIER_PORT must be an integer"):
        AtelierConfig.from_env(
            {
                "ATELIER_DATABASE_URL": "postgresql+asyncpg://a:b@localhost:5432/c",
                "ATELIER_PORT": "abc",
            }
        )


def test_bearer_mode_requires_token() -> None:
    with pytest.raises(ConfigError, match="bearer_token is required"):
        AtelierConfig.from_env(
            {
                "ATELIER_DATABASE_URL": "postgresql+asyncpg://a:b@localhost:5432/c",
                "ATELIER_AUTH_MODE": "bearer",
            }
        )


def test_bearer_mode_with_token_ok() -> None:
    cfg = AtelierConfig.from_env(
        {
            "ATELIER_DATABASE_URL": "postgresql+asyncpg://a:b@localhost:5432/c",
            "ATELIER_AUTH_MODE": "bearer",
            "ATELIER_BEARER_TOKEN": "secret",
        }
    )
    assert cfg.auth_mode == "bearer"
    assert cfg.bearer_token == "secret"


def test_unknown_auth_mode_rejected() -> None:
    with pytest.raises(ConfigError, match="unsupported auth_mode"):
        AtelierConfig.from_env(
            {
                "ATELIER_DATABASE_URL": "postgresql+asyncpg://a:b@localhost:5432/c",
                "ATELIER_AUTH_MODE": "magic",
            }
        )


def test_unknown_override_raises() -> None:
    with pytest.raises(ConfigError, match="unknown configuration overrides"):
        AtelierConfig.from_env(
            {"ATELIER_DATABASE_URL": "postgresql+asyncpg://a:b@localhost:5432/c"},
            nonsense=1,
        )


def test_field_names_includes_database_url() -> None:
    cfg = AtelierConfig.from_env(
        {"ATELIER_DATABASE_URL": "postgresql+asyncpg://a:b@localhost:5432/c"}
    )
    assert "database_url" in cfg.field_names()
    assert "auth_mode" in cfg.field_names()


# ---------------------------------------------------------------------------
# Stage G: agent backend / distill / memory toggles
# ---------------------------------------------------------------------------


_DB = "postgresql+asyncpg://a:b@localhost:5432/c"


def test_agent_backend_defaults_to_none() -> None:
    cfg = AtelierConfig.from_env({"ATELIER_DATABASE_URL": _DB})
    assert cfg.agent_backend == "none"
    assert cfg.agent_cwd is None
    assert cfg.agent_allowed_tools == ()


def test_agent_backend_requires_cwd() -> None:
    with pytest.raises(ConfigError, match="agent_cwd is required"):
        AtelierConfig.from_env(
            {
                "ATELIER_DATABASE_URL": _DB,
                "ATELIER_AGENT_BACKEND": "claude_code",
            }
        )


def test_agent_backend_with_cwd_ok() -> None:
    cfg = AtelierConfig.from_env(
        {
            "ATELIER_DATABASE_URL": _DB,
            "ATELIER_AGENT_BACKEND": "claude_code",
            "ATELIER_AGENT_CWD": "/tmp/workspace",
            "ATELIER_AGENT_ALLOWED_TOOLS": "shell.run, file.read ",
        }
    )
    assert cfg.agent_backend == "claude_code"
    assert cfg.agent_cwd == "/tmp/workspace"
    assert cfg.agent_allowed_tools == ("shell.run", "file.read")


def test_unknown_agent_backend_rejected() -> None:
    with pytest.raises(ConfigError, match="unsupported agent_backend"):
        AtelierConfig.from_env(
            {
                "ATELIER_DATABASE_URL": _DB,
                "ATELIER_AGENT_BACKEND": "magic",
            }
        )


def test_distill_enabled_requires_database_url() -> None:
    with pytest.raises(ConfigError, match="distill_database_url is required"):
        AtelierConfig.from_env(
            {
                "ATELIER_DATABASE_URL": _DB,
                "ATELIER_DISTILL_ENABLED": "true",
            }
        )


def test_distill_resolver_requires_distill_enabled() -> None:
    with pytest.raises(ConfigError, match="snapshot_resolver='distill' requires"):
        AtelierConfig.from_env(
            {
                "ATELIER_DATABASE_URL": _DB,
                "ATELIER_SNAPSHOT_RESOLVER": "distill",
            }
        )


def test_distill_full_config_ok() -> None:
    cfg = AtelierConfig.from_env(
        {
            "ATELIER_DATABASE_URL": _DB,
            "ATELIER_DISTILL_ENABLED": "true",
            "ATELIER_DISTILL_DATABASE_URL": "postgresql+asyncpg://d:d@localhost:5433/d",
            "ATELIER_SNAPSHOT_RESOLVER": "distill",
        }
    )
    assert cfg.distill_enabled is True
    assert cfg.snapshot_resolver == "distill"
    assert cfg.distill_auto_ingest is True
    assert cfg.agent_memory_enabled is True


def test_agent_memory_can_be_disabled() -> None:
    cfg = AtelierConfig.from_env(
        {
            "ATELIER_DATABASE_URL": _DB,
            "ATELIER_AGENT_MEMORY": "off",
        }
    )
    assert cfg.agent_memory_enabled is False
