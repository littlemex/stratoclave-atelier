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
    # Per-session cwd isolation is the safe default so siblings/branches
    # do not share Claude Code memory or other on-disk backend state.
    assert cfg.agent_cwd_isolation == "per_session"


def test_agent_cwd_isolation_shared_opt_in() -> None:
    cfg = AtelierConfig.from_env(
        {
            "ATELIER_DATABASE_URL": _DB,
            "ATELIER_AGENT_BACKEND": "claude_code",
            "ATELIER_AGENT_CWD": "/tmp/workspace",
            "ATELIER_AGENT_CWD_ISOLATION": "shared",
        }
    )
    assert cfg.agent_cwd_isolation == "shared"


def test_agent_cwd_isolation_invalid_rejected() -> None:
    with pytest.raises(ConfigError, match="unsupported agent_cwd_isolation"):
        AtelierConfig.from_env(
            {
                "ATELIER_DATABASE_URL": _DB,
                "ATELIER_AGENT_BACKEND": "claude_code",
                "ATELIER_AGENT_CWD": "/tmp/workspace",
                "ATELIER_AGENT_CWD_ISOLATION": "global",
            }
        )


def test_agent_cwd_inside_git_rejected_under_per_session(tmp_path: pytest.TempPathFactory) -> None:
    """Per-session isolation must refuse a cwd nested in a git repo.

    Claude Code keys auto-memory by the git root, not by the cwd, so
    putting agent_cwd inside a git checkout silently collapses every
    atelier session to a single shared memory dir -- exactly the
    cross-session contamination per-session isolation was supposed to
    prevent. Reject the combination at startup with a clear message.
    """

    repo = tmp_path / "repo"  # type: ignore[attr-defined]
    (repo / ".git").mkdir(parents=True)
    inner = repo / "wk"
    inner.mkdir()

    with pytest.raises(ConfigError, match="sits inside a git repository"):
        AtelierConfig.from_env(
            {
                "ATELIER_DATABASE_URL": _DB,
                "ATELIER_AGENT_BACKEND": "claude_code",
                "ATELIER_AGENT_CWD": str(inner),
            }
        )


def test_agent_cwd_inside_git_allowed_with_escape_hatch(
    tmp_path: pytest.TempPathFactory,
) -> None:
    """``ATELIER_ALLOW_AGENT_CWD_INSIDE_GIT=1`` opts back in, at user risk."""

    repo = tmp_path / "repo"  # type: ignore[attr-defined]
    (repo / ".git").mkdir(parents=True)
    inner = repo / "wk"
    inner.mkdir()

    cfg = AtelierConfig.from_env(
        {
            "ATELIER_DATABASE_URL": _DB,
            "ATELIER_AGENT_BACKEND": "claude_code",
            "ATELIER_AGENT_CWD": str(inner),
            "ATELIER_ALLOW_AGENT_CWD_INSIDE_GIT": "1",
        }
    )
    assert cfg.allow_agent_cwd_inside_git is True


def test_agent_cwd_inside_git_allowed_under_shared_isolation(
    tmp_path: pytest.TempPathFactory,
) -> None:
    """``shared`` isolation has no per-session promise to break, so the guard skips."""

    repo = tmp_path / "repo"  # type: ignore[attr-defined]
    (repo / ".git").mkdir(parents=True)
    inner = repo / "wk"
    inner.mkdir()

    cfg = AtelierConfig.from_env(
        {
            "ATELIER_DATABASE_URL": _DB,
            "ATELIER_AGENT_BACKEND": "claude_code",
            "ATELIER_AGENT_CWD": str(inner),
            "ATELIER_AGENT_CWD_ISOLATION": "shared",
        }
    )
    assert cfg.agent_cwd_isolation == "shared"


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


# ---------------------------------------------------------------------------
# Stage H: per-session backend selection + per-backend overrides
# ---------------------------------------------------------------------------


def test_resolved_backends_back_compat_with_singular() -> None:
    """Stage G env style still works: only ATELIER_AGENT_BACKEND set."""

    cfg = AtelierConfig.from_env(
        {
            "ATELIER_DATABASE_URL": _DB,
            "ATELIER_AGENT_BACKEND": "claude_code",
            "ATELIER_AGENT_CWD": "/tmp/wk",
        }
    )
    assert cfg.resolved_backends() == ("claude_code",)
    assert cfg.cwd_for_backend("claude_code") == "/tmp/wk"
    assert cfg.allowed_tools_for_backend("claude_code") == ()


def test_resolved_backends_none_collapses_to_empty() -> None:
    cfg = AtelierConfig.from_env({"ATELIER_DATABASE_URL": _DB})
    assert cfg.agent_backend == "none"
    assert cfg.resolved_backends() == ()


def test_agent_backends_allowed_csv_parsing() -> None:
    cfg = AtelierConfig.from_env(
        {
            "ATELIER_DATABASE_URL": _DB,
            "ATELIER_AGENT_BACKENDS_ALLOWED": " claude_code , kiro_code ",
            "ATELIER_AGENT_CWD": "/tmp/wk",
            "ATELIER_AGENT_BACKEND": "claude_code",
        }
    )
    assert cfg.agent_backends_allowed == ("claude_code", "kiro_code")
    assert cfg.resolved_backends() == ("claude_code", "kiro_code")


def test_per_backend_cwd_overrides_default() -> None:
    cfg = AtelierConfig.from_env(
        {
            "ATELIER_DATABASE_URL": _DB,
            "ATELIER_AGENT_BACKENDS_ALLOWED": "claude_code,kiro_code",
            "ATELIER_AGENT_CWD": "/tmp/default",
            "ATELIER_AGENT_CWD_KIRO_CODE": "/tmp/kiro",
            "ATELIER_AGENT_BACKEND": "claude_code",
        }
    )
    assert cfg.cwd_for_backend("claude_code") == "/tmp/default"
    assert cfg.cwd_for_backend("kiro_code") == "/tmp/kiro"


def test_per_backend_allowed_tools_overrides_default() -> None:
    cfg = AtelierConfig.from_env(
        {
            "ATELIER_DATABASE_URL": _DB,
            "ATELIER_AGENT_BACKENDS_ALLOWED": "claude_code,kiro_code",
            "ATELIER_AGENT_CWD": "/tmp/wk",
            "ATELIER_AGENT_ALLOWED_TOOLS": "shell.run",
            "ATELIER_AGENT_ALLOWED_TOOLS_KIRO_CODE": "file.read, file.write",
            "ATELIER_AGENT_BACKEND": "claude_code",
        }
    )
    assert cfg.allowed_tools_for_backend("claude_code") == ("shell.run",)
    assert cfg.allowed_tools_for_backend("kiro_code") == ("file.read", "file.write")


def test_unknown_backend_in_allowed_list_rejected() -> None:
    with pytest.raises(ConfigError, match="unsupported backend in agent_backends_allowed"):
        AtelierConfig.from_env(
            {
                "ATELIER_DATABASE_URL": _DB,
                "ATELIER_AGENT_BACKENDS_ALLOWED": "claude_code,wizard",
                "ATELIER_AGENT_CWD": "/tmp/wk",
            }
        )


def test_allowed_backend_without_cwd_rejected() -> None:
    with pytest.raises(ConfigError, match=r"agent_cwd is required for backend 'kiro_code'"):
        AtelierConfig.from_env(
            {
                "ATELIER_DATABASE_URL": _DB,
                "ATELIER_AGENT_BACKENDS_ALLOWED": "claude_code,kiro_code",
                "ATELIER_AGENT_CWD_CLAUDE_CODE": "/tmp/cc",
                # kiro_code has no cwd configured at all -> reject.
            }
        )


def test_default_backend_must_be_in_allowed_list() -> None:
    with pytest.raises(ConfigError, match="must appear in agent_backends_allowed"):
        AtelierConfig.from_env(
            {
                "ATELIER_DATABASE_URL": _DB,
                "ATELIER_AGENT_BACKENDS_ALLOWED": "kiro_code",
                "ATELIER_AGENT_CWD": "/tmp/wk",
                "ATELIER_AGENT_BACKEND": "claude_code",
            }
        )


def test_per_backend_cwd_only_no_default_ok() -> None:
    """Allowed backends only need *some* cwd -- per-backend is enough."""

    cfg = AtelierConfig.from_env(
        {
            "ATELIER_DATABASE_URL": _DB,
            "ATELIER_AGENT_BACKENDS_ALLOWED": "claude_code,kiro_code",
            "ATELIER_AGENT_CWD_CLAUDE_CODE": "/tmp/cc",
            "ATELIER_AGENT_CWD_KIRO_CODE": "/tmp/kc",
        }
    )
    # No default agent_backend: agent_backend defaults to 'none' but the
    # picker sees both backends.
    assert cfg.agent_backend == "none"
    assert cfg.resolved_backends() == ("claude_code", "kiro_code")
    assert cfg.cwd_for_backend("claude_code") == "/tmp/cc"
    assert cfg.cwd_for_backend("kiro_code") == "/tmp/kc"


def test_per_backend_overrides_via_kwargs() -> None:
    cfg = AtelierConfig.from_env(
        {"ATELIER_DATABASE_URL": _DB},
        agent_backends_allowed=("claude_code", "mock"),
        agent_cwd_by_backend={"claude_code": "/wk/cc", "mock": "/wk/mock"},
        agent_allowed_tools_by_backend={"claude_code": ("shell.run",), "mock": "noop.run"},
    )
    assert cfg.cwd_for_backend("claude_code") == "/wk/cc"
    assert cfg.cwd_for_backend("mock") == "/wk/mock"
    assert cfg.allowed_tools_for_backend("claude_code") == ("shell.run",)
    assert cfg.allowed_tools_for_backend("mock") == ("noop.run",)
