"""Pytest fixtures shared across the atelier test suite."""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from stratoclave_atelier.config import AtelierConfig


@pytest.fixture
def stub_env() -> Mapping[str, str]:
    """Minimum env required to construct an AtelierConfig in tests.

    Tests that need richer config should override individual keys via
    ``AtelierConfig.from_env({**stub_env, "ATELIER_PORT": "9000"})`` or
    by passing kwargs to :meth:`AtelierConfig.from_env`.
    """

    return {
        "ATELIER_DATABASE_URL": ("postgresql+asyncpg://atelier:atelier@localhost:5432/atelier"),
    }


@pytest.fixture
def stub_config(stub_env: Mapping[str, str]) -> AtelierConfig:
    """An AtelierConfig built from :func:`stub_env`."""

    return AtelierConfig.from_env(stub_env)
