"""Unit tests for :mod:`stratoclave_atelier.auto_namer`."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from stratoclave_atelier.auto_namer import (
    LoomAutoNamer,
    NoopAutoNamer,
    _clamp_title,
    _clean_loom_output,
    _format_turns,
    build_auto_namer,
)
from stratoclave_atelier.config import AtelierConfig
from stratoclave_atelier.core.types import Event, Session


def _make_session(title: str = "main", backend: str | None = None) -> Session:
    now = datetime.now(UTC)
    return Session(
        session_id=uuid4(),
        group_id=None,
        title=title,
        parent_session_id=None,
        parent_version_id=None,
        fork_seq=None,
        status="active",
        created_at=now,
        updated_at=now,
        agent_backend=backend,
    )


def _make_event(seq: int, role: str, content: str, kind: str = "turn") -> Event:
    return Event(
        event_id=uuid4(),
        session_id=uuid4(),
        seq=seq,
        kind=kind,  # type: ignore[arg-type]
        payload={"kind": kind, "role": role, "content": content},
        created_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_noop_namer_appends_random_suffix() -> None:
    namer = NoopAutoNamer()
    parent = _make_session(title="atelier-main")

    one = await namer.name_branch(parent=parent, recent_events=[])
    two = await namer.name_branch(parent=parent, recent_events=[])

    assert one.startswith("atelier-main-")
    assert two.startswith("atelier-main-")
    assert one != two  # 4-hex suffix should differ across calls
    assert namer.enabled is False


@pytest.mark.asyncio
async def test_noop_namer_falls_back_to_branch_when_title_empty() -> None:
    namer = NoopAutoNamer()
    parent = _make_session(title="")

    out = await namer.name_branch(parent=parent, recent_events=[])

    assert out.startswith("branch-")


@pytest.mark.asyncio
async def test_noop_namer_clamps_runaway_parent_title() -> None:
    namer = NoopAutoNamer()
    parent = _make_session(title="x" * 200)

    out = await namer.name_branch(parent=parent, recent_events=[])

    assert len(out) <= 60
    assert out.endswith("…")


def test_clean_loom_output_strips_quotes_and_punct() -> None:
    assert _clean_loom_output('  "API design questions."\n') == "API design questions"
    assert _clean_loom_output("```\nslide draft\n```") == "slide draft"
    assert _clean_loom_output("first line\nsecond line") == "first line"


def test_clamp_title_inserts_ellipsis() -> None:
    long = "a" * 80
    out = _clamp_title(long)
    assert len(out) == 60
    assert out.endswith("…")


def test_format_turns_handles_mixed_kinds_and_truncation() -> None:
    events = [
        _make_event(1, "user", "hi", kind="turn"),
        _make_event(2, "assistant", "hello!", kind="agent_turn"),
        _make_event(3, "system", "ignored", kind="freeze"),
        _make_event(4, "user", "x" * 400, kind="turn"),
    ]

    rendered = _format_turns(events)

    assert rendered.startswith("user: hi")
    assert "assistant: hello!" in rendered
    assert "ignored" not in rendered
    assert "..." in rendered  # last user turn truncated


def test_format_turns_empty_renders_placeholder() -> None:
    assert _format_turns([]) == "(no recent turns)"


@pytest.mark.asyncio
async def test_build_auto_namer_returns_noop_when_no_backend(
    stub_env: Mapping[str, str],
) -> None:
    cfg = AtelierConfig.from_env(stub_env)
    namer = build_auto_namer(cfg)
    assert isinstance(namer, NoopAutoNamer)


@pytest.mark.asyncio
async def test_build_auto_namer_returns_loom_when_backend_set(
    stub_env: Mapping[str, str], tmp_path: Any
) -> None:
    cfg = AtelierConfig.from_env(
        {
            **stub_env,
            "ATELIER_AGENT_BACKEND": "claude_code",
            "ATELIER_AGENT_CWD": str(tmp_path),
        }
    )
    namer = build_auto_namer(cfg)
    assert isinstance(namer, LoomAutoNamer)
    assert namer.enabled is True


class _StubLoomSession:
    """Stand-in for stratoclave_loom.AgentSession that emits scripted chunks."""

    def __init__(self, *, chunks: list[tuple[str, dict[str, Any]]]) -> None:
        self._chunks = chunks
        self.closed = False

    async def send_message(self, prompt: str) -> Any:
        del prompt

        async def _gen() -> Any:
            for kind, payload in self._chunks:
                yield _StubChunk(chunk_type=kind, content=payload)

        return _gen()

    async def close(self) -> None:
        self.closed = True


class _StubChunk:
    def __init__(self, *, chunk_type: str, content: dict[str, Any]) -> None:
        self.chunk_type = chunk_type
        self.content = content


@pytest.mark.asyncio
async def test_loom_namer_returns_clean_title(
    stub_env: Mapping[str, str],
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = AtelierConfig.from_env(
        {
            **stub_env,
            "ATELIER_AGENT_BACKEND": "claude_code",
            "ATELIER_AGENT_CWD": str(tmp_path),
        }
    )
    namer = LoomAutoNamer(config=cfg)
    session_stub = _StubLoomSession(
        chunks=[
            ("text_delta", {"text": '"API design '}),
            ("text_delta", {"text": "questions."}),
            ("end_turn", {}),
        ]
    )

    def _fake_create(_cfg: Any, **_kwargs: Any) -> _StubLoomSession:
        return session_stub

    monkeypatch.setattr("stratoclave_loom.create_session", _fake_create)

    parent = _make_session(title="atelier-main", backend="claude_code")
    title = await namer.name_branch(parent=parent, recent_events=[])

    assert title == "API design questions"
    assert session_stub.closed is True


@pytest.mark.asyncio
async def test_loom_namer_raises_on_empty_response(
    stub_env: Mapping[str, str],
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = AtelierConfig.from_env(
        {
            **stub_env,
            "ATELIER_AGENT_BACKEND": "claude_code",
            "ATELIER_AGENT_CWD": str(tmp_path),
        }
    )
    namer = LoomAutoNamer(config=cfg)
    session_stub = _StubLoomSession(chunks=[("end_turn", {})])
    monkeypatch.setattr("stratoclave_loom.create_session", lambda _cfg, **_k: session_stub)

    parent = _make_session(title="atelier-main", backend="claude_code")
    with pytest.raises(RuntimeError, match="empty/too-short"):
        await namer.name_branch(parent=parent, recent_events=[])


@pytest.mark.asyncio
async def test_loom_namer_propagates_error_chunk(
    stub_env: Mapping[str, str],
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = AtelierConfig.from_env(
        {
            **stub_env,
            "ATELIER_AGENT_BACKEND": "claude_code",
            "ATELIER_AGENT_CWD": str(tmp_path),
        }
    )
    namer = LoomAutoNamer(config=cfg)
    session_stub = _StubLoomSession(chunks=[("error", {"message": "loom blew up"})])
    monkeypatch.setattr("stratoclave_loom.create_session", lambda _cfg, **_k: session_stub)

    parent = _make_session(title="atelier-main", backend="claude_code")
    with pytest.raises(RuntimeError, match="loom returned error"):
        await namer.name_branch(parent=parent, recent_events=[])


@pytest.mark.asyncio
async def test_loom_namer_times_out(
    stub_env: Mapping[str, str],
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = AtelierConfig.from_env(
        {
            **stub_env,
            "ATELIER_AGENT_BACKEND": "claude_code",
            "ATELIER_AGENT_CWD": str(tmp_path),
        }
    )
    namer = LoomAutoNamer(config=cfg)

    class _StallSession:
        async def send_message(self, prompt: str) -> Any:
            await asyncio.sleep(5)

            async def _gen() -> Any:
                if False:
                    yield None

            return _gen()

        async def close(self) -> None:
            return None

    monkeypatch.setattr("stratoclave_atelier.auto_namer._TITLE_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr("stratoclave_loom.create_session", lambda _cfg, **_k: _StallSession())

    parent = _make_session(title="atelier-main", backend="claude_code")
    with pytest.raises(asyncio.TimeoutError):
        await namer.name_branch(parent=parent, recent_events=[])


@pytest.mark.asyncio
async def test_loom_namer_uses_parent_backend_when_set(
    stub_env: Mapping[str, str],
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = AtelierConfig.from_env(
        {
            **stub_env,
            "ATELIER_AGENT_BACKENDS_ALLOWED": "claude_code,kiro_code",
            "ATELIER_AGENT_BACKEND": "claude_code",
            "ATELIER_AGENT_CWD": str(tmp_path),
        }
    )
    namer = LoomAutoNamer(config=cfg)
    seen_backends: list[str] = []

    session_stub = _StubLoomSession(
        chunks=[
            ("text_delta", {"text": "kiro flavored"}),
            ("end_turn", {}),
        ]
    )

    def _fake_create(loom_cfg: Any, **_kwargs: Any) -> _StubLoomSession:
        seen_backends.append(loom_cfg.backend)
        return session_stub

    monkeypatch.setattr("stratoclave_loom.create_session", _fake_create)

    parent = _make_session(title="atelier-main", backend="kiro_code")
    title = await namer.name_branch(parent=parent, recent_events=[])

    assert title == "kiro flavored"
    assert seen_backends == ["kiro_code"]
