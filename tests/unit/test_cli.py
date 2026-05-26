"""Tests for the ``stratoclave-atelier`` CLI surface."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

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


# ---------------------------------------------------------------------------
# Stage F: session subcommands -- stub httpx and assert the wire calls.
# ---------------------------------------------------------------------------


class _StubResponse:
    def __init__(self, *, status_code: int = 200, body: Any = None, raw: bytes = b"") -> None:
        self.status_code = status_code
        self._body = body
        self.content = raw if raw or body is None else b"x"
        self.text = "" if body is None else "stub-text"

    def json(self) -> Any:
        return self._body


class _StubClient:
    """Records the most recent ``request`` call for assertions."""

    last_call: ClassVar[dict[str, Any]] = {}
    next_response: ClassVar[_StubResponse | None] = None

    def __init__(self, *, timeout: float) -> None:
        _StubClient.last_call["timeout"] = timeout

    def __enter__(self) -> _StubClient:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def request(
        self,
        method: str,
        url: str,
        *,
        json: Any = None,
        params: Any = None,
    ) -> _StubResponse:
        _StubClient.last_call.update(
            method=method,
            url=url,
            json=json,
            params=params,
        )
        assert _StubClient.next_response is not None, "test must set next_response"
        return _StubClient.next_response


@pytest.fixture
def stub_httpx(monkeypatch: pytest.MonkeyPatch) -> type[_StubClient]:
    import httpx

    monkeypatch.setattr(httpx, "Client", _StubClient)
    _StubClient.last_call = {}
    _StubClient.next_response = None
    return _StubClient


def test_session_list_uses_default_base_url(
    stub_httpx: type[_StubClient],
    capsys: pytest.CaptureFixture[str],
) -> None:
    stub_httpx.next_response = _StubResponse(body=[{"session_id": "abc"}])
    rc = cli.main(["session", "list"])
    assert rc == 0
    assert stub_httpx.last_call["method"] == "GET"
    assert stub_httpx.last_call["url"] == "http://localhost:8000/api/sessions"
    assert stub_httpx.last_call["params"] is None
    out = capsys.readouterr().out
    assert "abc" in out


def test_session_list_honours_base_url_flag(
    stub_httpx: type[_StubClient],
) -> None:
    stub_httpx.next_response = _StubResponse(body=[])
    rc = cli.main(["session", "--base-url", "http://example.invalid:9999", "list"])
    assert rc == 0
    assert stub_httpx.last_call["url"] == "http://example.invalid:9999/api/sessions"


def test_session_list_honours_base_url_env(
    monkeypatch: pytest.MonkeyPatch,
    stub_httpx: type[_StubClient],
) -> None:
    monkeypatch.setenv("ATELIER_BASE_URL", "http://from-env.local:7000")
    stub_httpx.next_response = _StubResponse(body=[])
    rc = cli.main(["session", "list"])
    assert rc == 0
    assert stub_httpx.last_call["url"] == "http://from-env.local:7000/api/sessions"


def test_session_list_with_group_id(
    stub_httpx: type[_StubClient],
) -> None:
    stub_httpx.next_response = _StubResponse(body=[])
    rc = cli.main(["session", "list", "--group-id", "g-1"])
    assert rc == 0
    assert stub_httpx.last_call["params"] == {"group_id": "g-1"}


def test_session_show_emits_session_and_versions(
    stub_httpx: type[_StubClient],
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Two sequential calls: GET session, GET versions. Use side-effect via
    # mutating next_response inside request(); easier: stub returns the
    # same body each call but we only check the last url here.
    stub_httpx.next_response = _StubResponse(body={"session_id": "s-1"})
    rc = cli.main(["session", "show", "s-1"])
    assert rc == 0
    # The last call recorded is the versions GET.
    assert stub_httpx.last_call["url"].endswith("/api/sessions/s-1/versions")
    out = capsys.readouterr().out
    assert "session" in out
    assert "versions" in out


def test_session_send_turn_posts_role_and_content(
    stub_httpx: type[_StubClient],
) -> None:
    stub_httpx.next_response = _StubResponse(
        status_code=201,
        body={"event_id": "e-1", "kind": "turn"},
    )
    rc = cli.main(["session", "send-turn", "s-1", "--role", "user", "--content", "hi"])
    assert rc == 0
    assert stub_httpx.last_call["method"] == "POST"
    assert stub_httpx.last_call["url"].endswith("/api/sessions/s-1/turns")
    assert stub_httpx.last_call["json"] == {"role": "user", "content": "hi"}


def test_session_freeze_omits_unset_optionals(
    stub_httpx: type[_StubClient],
) -> None:
    stub_httpx.next_response = _StubResponse(status_code=201, body={"version_id": "v-1"})
    rc = cli.main(["session", "freeze", "s-1"])
    assert rc == 0
    assert stub_httpx.last_call["url"].endswith("/api/sessions/s-1/freeze")
    assert stub_httpx.last_call["json"] == {}


def test_session_freeze_passes_explicit_range(
    stub_httpx: type[_StubClient],
) -> None:
    stub_httpx.next_response = _StubResponse(status_code=201, body={"version_id": "v-1"})
    rc = cli.main(
        [
            "session",
            "freeze",
            "s-1",
            "--start-seq",
            "0",
            "--end-seq",
            "5",
            "--label",
            "demo",
        ]
    )
    assert rc == 0
    assert stub_httpx.last_call["json"] == {
        "start_seq": 0,
        "end_seq": 5,
        "label": "demo",
    }


def test_session_fork_passes_required_fields(
    stub_httpx: type[_StubClient],
) -> None:
    stub_httpx.next_response = _StubResponse(status_code=201, body={"session_id": "child"})
    rc = cli.main(
        [
            "session",
            "fork",
            "parent-1",
            "--title",
            "child",
            "--parent-version-id",
            "v-1",
            "--fork-seq",
            "3",
        ]
    )
    assert rc == 0
    assert stub_httpx.last_call["url"].endswith("/api/sessions/parent-1/fork")
    assert stub_httpx.last_call["json"] == {
        "title": "child",
        "parent_version_id": "v-1",
        "fork_seq": 3,
    }


def test_session_snapshot_query_posts_body(
    stub_httpx: type[_StubClient],
) -> None:
    stub_httpx.next_response = _StubResponse(status_code=201, body={"query_id": "q-1"})
    rc = cli.main(
        [
            "session",
            "snapshot-query",
            "s-1",
            "--target-version-id",
            "v-1",
            "--query",
            "what changed?",
        ]
    )
    assert rc == 0
    assert stub_httpx.last_call["url"].endswith("/api/sessions/s-1/snapshot-query")
    assert stub_httpx.last_call["json"] == {
        "target_version_id": "v-1",
        "query": "what changed?",
    }


def test_session_request_error_exits_nonzero(
    stub_httpx: type[_StubClient],
    capsys: pytest.CaptureFixture[str],
) -> None:
    stub_httpx.next_response = _StubResponse(status_code=409, body={"detail": "session is frozen"})
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["session", "send-turn", "s-1", "--content", "x"])
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "409" in err


def test_serve_in_memory_sets_placeholder_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``serve --in-memory`` should set a placeholder URL when none is provided."""

    monkeypatch.delenv("ATELIER_DATABASE_URL", raising=False)
    captured: dict[str, Any] = {}

    class _StubUvicorn:
        @staticmethod
        def run(app: Any, **kwargs: Any) -> None:
            captured["app"] = app
            captured["kwargs"] = kwargs

    monkeypatch.setitem(__import__("sys").modules, "uvicorn", _StubUvicorn)

    rc = cli.main(["serve", "--in-memory", "--port", "8123"])
    assert rc == 0
    # Placeholder is wired into the env so AtelierConfig.from_env succeeds.
    import os

    assert os.environ.get("ATELIER_DATABASE_URL", "").startswith("postgresql+asyncpg://")
    assert captured["kwargs"]["port"] == 8123
