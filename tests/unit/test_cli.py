"""Tests for the ``stratoclave-atelier`` CLI surface."""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from stratoclave_atelier import cli


def test_cli_version_short_circuits(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--version"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "stratoclave-atelier" in out


def test_cli_no_command_errors() -> None:
    with pytest.raises(SystemExit):
        cli.main([])


def test_config_subcommand_prints_keys(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stub_env: Mapping[str, str],
) -> None:
    for k, v in stub_env.items():
        monkeypatch.setenv(k, v)
    rc = cli.main(["config"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "database_url=" in out
    assert "auth_mode=" in out


def test_config_subcommand_redacts_bearer_token(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stub_env: Mapping[str, str],
) -> None:
    for k, v in stub_env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("ATELIER_AUTH_MODE", "bearer")
    monkeypatch.setenv("ATELIER_BEARER_TOKEN", "supersecret")
    rc = cli.main(["config"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "supersecret" not in out
    assert "bearer_token=<set," in out
