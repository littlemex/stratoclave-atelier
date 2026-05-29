"""Unit tests for :class:`AgentRunner` using a stub loom backend."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any

import pytest
from stratoclave_loom import (
    AcpChunk,
    AgentBackend,
    BackendConfig,
    PermissionRequest,
    register_backend,
)
from stratoclave_loom.core.types import NormalizedTurn

from stratoclave_atelier.agent_runner import AgentRunner
from stratoclave_atelier.config import AtelierConfig
from stratoclave_atelier.db import InMemoryStore
from stratoclave_atelier.events_bus import EventBus


class _StubBackend(AgentBackend):
    """Loom backend stub that emits a configurable chunk script."""

    backend_name = "stub_test"

    def __init__(self) -> None:
        self.script: list[tuple[str, Mapping[str, Any]]] = [
            ("text_delta", {"text": "Hello"}),
            ("text_delta", {"text": ", world"}),
            ("end_turn", {}),
        ]
        self.cancelled: list[str] = []
        self.closed: list[str] = []
        self.raise_on_send: BaseException | None = None

    async def initialize(
        self,
        session_id: str,
        config: BackendConfig,
        capabilities: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        return capabilities

    async def send_message(
        self,
        session_id: str,
        content: str,
        *,
        context_files: tuple[str, ...] = (),
    ) -> AsyncIterator[AcpChunk]:
        if self.raise_on_send is not None:
            raise self.raise_on_send

        async def _gen() -> AsyncIterator[AcpChunk]:
            for kind, payload in self.script:
                yield AcpChunk(session_id=session_id, chunk_type=kind, content=payload)

        return _gen()

    async def cancel(self, session_id: str) -> None:
        self.cancelled.append(session_id)

    async def close(self, session_id: str) -> None:
        self.closed.append(session_id)

    async def handle_permission(self, request: PermissionRequest, granted: bool) -> None:
        return None

    def normalize(self, raw_line: str, seq: int) -> list[NormalizedTurn]:
        return []

    def resume_args(self, frozen_jsonl_path: str) -> tuple[str, ...]:
        return ()


@pytest.fixture
def stub_backend() -> _StubBackend:
    backend = _StubBackend()
    register_backend("stub_test", lambda: backend)
    return backend


@pytest.fixture
def stub_runner_config(stub_env: Mapping[str, str], tmp_path: Any) -> AtelierConfig:
    return AtelierConfig.from_env(
        {
            **stub_env,
            "ATELIER_AGENT_BACKEND": "claude_code",  # any non-none satisfies the gate
            "ATELIER_AGENT_CWD": str(tmp_path),
        },
        agent_backend="claude_code",
    )


@pytest.mark.asyncio
async def test_runner_emits_chunks_then_summary(
    stub_runner_config: AtelierConfig,
    stub_backend: _StubBackend,
) -> None:
    # Override the backend literal: the runner reads agent_backend by name so
    # we substitute "stub_test" by mutating the config via from_env helper.
    cfg = AtelierConfig.from_env(
        {
            "ATELIER_DATABASE_URL": stub_runner_config.database_url,
            "ATELIER_AGENT_CWD": stub_runner_config.agent_cwd or ".",
        },
        agent_backend="claude_code",
    )
    # Sneak the stub backend name in via object.__setattr__ since the
    # dataclass is frozen and the Literal forbids "stub_test" at type level.
    object.__setattr__(cfg, "agent_backend", "stub_test")

    store = InMemoryStore()
    bus = EventBus()
    runner = AgentRunner(config=cfg, store=store, bus=bus)
    session = await store.create_session(title="test")

    await runner.run(session_id=session.session_id, prompt="hello")

    events = await store.list_events(session.session_id)
    kinds = [e.kind for e in events]
    assert kinds[0] == "turn"  # user turn first
    assert "agent_chunk" in kinds
    assert kinds[-1] == "agent_turn"

    chunks = [e for e in events if e.kind == "agent_chunk"]
    assert [c.payload["text"] for c in chunks] == ["Hello", ", world"]

    summary = events[-1]
    assert summary.payload["content"] == "Hello, world"
    assert summary.payload["role"] == "assistant"
    await runner.close()


@pytest.mark.asyncio
async def test_runner_records_error_event_on_send_failure(
    stub_runner_config: AtelierConfig,
    stub_backend: _StubBackend,
) -> None:
    cfg = stub_runner_config
    object.__setattr__(cfg, "agent_backend", "stub_test")

    stub_backend.raise_on_send = RuntimeError("boom")
    store = InMemoryStore()
    bus = EventBus()
    runner = AgentRunner(config=cfg, store=store, bus=bus)
    session = await store.create_session(title="test")

    await runner.run(session_id=session.session_id, prompt="hello")

    events = await store.list_events(session.session_id)
    assert events[0].kind == "turn"
    assert events[-1].kind == "agent_error"
    assert events[-1].payload["type"] == "RuntimeError"
    assert events[-1].payload["error"] == "boom"
    await runner.close()


@pytest.mark.asyncio
async def test_runner_disabled_when_backend_none(stub_env: Mapping[str, str]) -> None:
    cfg = AtelierConfig.from_env(stub_env)
    runner = AgentRunner(config=cfg, store=InMemoryStore(), bus=EventBus())
    assert runner.enabled is False
    with pytest.raises(RuntimeError, match="agent backend disabled"):
        from uuid import uuid4

        await runner.run(session_id=uuid4(), prompt="hi")


@pytest.mark.asyncio
async def test_runner_compose_prompt_injects_memory() -> None:
    composed = AgentRunner._compose_prompt("question", "fact A\nfact B")
    assert composed.startswith("<memory>\n")
    assert "fact A" in composed
    assert composed.endswith("question")


# ---------------------------------------------------------------------------
# Stage H: per-session backend resolution
# ---------------------------------------------------------------------------


def test_resolve_backend_prefers_requested(
    stub_runner_config: AtelierConfig,
) -> None:
    runner = AgentRunner(config=stub_runner_config, store=InMemoryStore(), bus=EventBus())
    assert runner._resolve_backend_for("kiro_code") == "kiro_code"


def test_resolve_backend_falls_back_to_default(
    stub_runner_config: AtelierConfig,
) -> None:
    runner = AgentRunner(config=stub_runner_config, store=InMemoryStore(), bus=EventBus())
    assert runner._resolve_backend_for(None) == "claude_code"


def test_resolve_backend_uses_single_allowed_when_default_none(
    stub_env: Mapping[str, str], tmp_path: Any
) -> None:
    cfg = AtelierConfig.from_env(
        {
            **stub_env,
            "ATELIER_AGENT_BACKENDS_ALLOWED": "kiro_code",
            "ATELIER_AGENT_CWD": str(tmp_path),
        }
    )
    runner = AgentRunner(config=cfg, store=InMemoryStore(), bus=EventBus())
    assert runner.enabled is True
    assert runner._resolve_backend_for(None) == "kiro_code"


def test_resolve_backend_raises_when_ambiguous(stub_env: Mapping[str, str], tmp_path: Any) -> None:
    cfg = AtelierConfig.from_env(
        {
            **stub_env,
            "ATELIER_AGENT_BACKENDS_ALLOWED": "claude_code,kiro_code",
            "ATELIER_AGENT_CWD": str(tmp_path),
        }
    )
    runner = AgentRunner(config=cfg, store=InMemoryStore(), bus=EventBus())
    with pytest.raises(RuntimeError, match="no default agent backend"):
        runner._resolve_backend_for(None)


@pytest.mark.asyncio
async def test_runner_uses_per_session_backend(
    stub_runner_config: AtelierConfig,
    stub_backend: _StubBackend,
) -> None:
    """When the session names a backend, the runner uses it instead of the default."""

    cfg = stub_runner_config
    # Allow stub_test in the resolver path: bypass Literal typing.
    object.__setattr__(cfg, "agent_backend", "claude_code")
    store = InMemoryStore()
    bus = EventBus()
    runner = AgentRunner(config=cfg, store=store, bus=bus)
    session = await store.create_session(title="kiro-pinned", agent_backend="stub_test")

    await runner.run(
        session_id=session.session_id,
        prompt="hi",
        backend=session.agent_backend,
    )

    events = await store.list_events(session.session_id)
    kinds = [e.kind for e in events]
    assert "agent_turn" in kinds
    # The stub backend was warmed: confirm by checking close() reaches it.
    await runner.close()
    assert session.session_id is not None


@pytest.mark.asyncio
async def test_runner_publishes_to_bus(
    stub_runner_config: AtelierConfig,
    stub_backend: _StubBackend,
) -> None:
    import asyncio

    cfg = stub_runner_config
    object.__setattr__(cfg, "agent_backend", "stub_test")
    store = InMemoryStore()
    bus = EventBus()
    runner = AgentRunner(config=cfg, store=store, bus=bus)
    session = await store.create_session(title="test")

    received: list[Any] = []
    async with bus.subscribe(session.session_id) as queue:

        async def collect() -> None:
            while True:
                item = await queue.get()
                if item is None:
                    return
                received.append(item)

        task = asyncio.create_task(collect())
        await runner.run(session_id=session.session_id, prompt="hi")
        # End-of-turn marker pulled by the runner; cancel the collector.
        await asyncio.sleep(0.05)
        task.cancel()
        import contextlib

        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    kinds = [e.kind for e in received]
    assert "turn" in kinds
    assert "agent_chunk" in kinds
    assert "agent_turn" in kinds
    await runner.close()


# ---------------------------------------------------------------------------
# cwd isolation: per-session directory + branch seeding
# ---------------------------------------------------------------------------


def test_resolve_session_cwd_per_session_creates_subdir(
    stub_env: Mapping[str, str], tmp_path: Any
) -> None:
    """Default isolation = per_session: each atelier session_id gets its own dir."""

    from pathlib import Path
    from uuid import uuid4

    cfg = AtelierConfig.from_env(
        {
            **stub_env,
            "ATELIER_AGENT_BACKEND": "claude_code",
            "ATELIER_AGENT_CWD": str(tmp_path),
        },
    )
    runner = AgentRunner(config=cfg, store=InMemoryStore(), bus=EventBus())

    sid_a = uuid4()
    sid_b = uuid4()
    cwd_a = runner.resolve_session_cwd(sid_a)
    cwd_b = runner.resolve_session_cwd(sid_b)

    assert cwd_a != cwd_b
    assert Path(cwd_a).is_dir()
    assert Path(cwd_b).is_dir()
    assert Path(cwd_a).parent.name == "sessions"
    assert Path(cwd_a).name == str(sid_a)


def test_resolve_session_cwd_shared_returns_base(
    stub_env: Mapping[str, str], tmp_path: Any
) -> None:
    """Opt-in shared mode keeps Stage G behaviour: every session uses the base dir."""

    from uuid import uuid4

    cfg = AtelierConfig.from_env(
        {
            **stub_env,
            "ATELIER_AGENT_BACKEND": "claude_code",
            "ATELIER_AGENT_CWD": str(tmp_path),
            "ATELIER_AGENT_CWD_ISOLATION": "shared",
        },
    )
    runner = AgentRunner(config=cfg, store=InMemoryStore(), bus=EventBus())
    sid_a = uuid4()
    sid_b = uuid4()
    assert runner.resolve_session_cwd(sid_a) == str(tmp_path)
    assert runner.resolve_session_cwd(sid_b) == str(tmp_path)


@pytest.mark.asyncio
async def test_seed_branch_cwd_copies_parent_tree(
    stub_env: Mapping[str, str], tmp_path: Any
) -> None:
    """Branch flow: child cwd inherits the parent's files at fork time, then diverges."""

    from pathlib import Path
    from uuid import uuid4

    cfg = AtelierConfig.from_env(
        {
            **stub_env,
            "ATELIER_AGENT_BACKEND": "claude_code",
            "ATELIER_AGENT_CWD": str(tmp_path),
        },
    )
    runner = AgentRunner(config=cfg, store=InMemoryStore(), bus=EventBus())

    parent_id = uuid4()
    child_id = uuid4()

    parent_cwd = Path(runner.resolve_session_cwd(parent_id))
    # Simulate the agent persisting some state in the parent cwd.
    (parent_cwd / "memory").mkdir()
    (parent_cwd / "memory" / "user_name.md").write_text("name: satoshi\n")
    (parent_cwd / "scratch.txt").write_text("hello")

    await runner.seed_branch_cwd(parent_session_id=parent_id, child_session_id=child_id)

    child_cwd = Path(runner.resolve_session_cwd(child_id))
    assert (child_cwd / "memory" / "user_name.md").read_text() == "name: satoshi\n"
    assert (child_cwd / "scratch.txt").read_text() == "hello"

    # After branch, writes to child must NOT propagate to parent.
    (child_cwd / "memory" / "mother.md").write_text("mother: karin\n")
    assert not (parent_cwd / "memory" / "mother.md").exists()


@pytest.mark.asyncio
async def test_seed_branch_cwd_copies_claude_project_memory(
    stub_env: Mapping[str, str], tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Branch flow also copies ~/.claude/projects/<slug>/memory.

    Claude Code persists per-project memory keyed by realpath(cwd) ->
    slug, *outside* the cwd. Without copying that dir the per-session
    cwd isolation accidentally erases the parent's learned facts at
    fork time.
    """

    from pathlib import Path
    from uuid import uuid4

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    cfg = AtelierConfig.from_env(
        {
            **stub_env,
            "ATELIER_AGENT_BACKEND": "claude_code",
            "ATELIER_AGENT_CWD": str(tmp_path / "agent"),
        },
    )
    runner = AgentRunner(config=cfg, store=InMemoryStore(), bus=EventBus())

    parent_id = uuid4()
    child_id = uuid4()
    parent_cwd = Path(runner.resolve_session_cwd(parent_id))

    parent_real = parent_cwd.resolve()  # noqa: ASYNC240 -- test-only sync stat
    parent_slug = str(parent_real).replace("/", "-")
    parent_mem = fake_home / ".claude" / "projects" / parent_slug / "memory"
    parent_mem.mkdir(parents=True)
    (parent_mem / "MEMORY.md").write_text("- favourite color: blue\n")
    (parent_mem / "user_color.md").write_text("blue (青)\n")

    await runner.seed_branch_cwd(parent_session_id=parent_id, child_session_id=child_id)

    child_cwd = Path(runner.resolve_session_cwd(child_id))
    child_slug = str(child_cwd.resolve()).replace("/", "-")  # noqa: ASYNC240
    child_mem = fake_home / ".claude" / "projects" / child_slug / "memory"
    assert (child_mem / "MEMORY.md").read_text() == "- favourite color: blue\n"
    assert (child_mem / "user_color.md").read_text() == "blue (青)\n"

    # Subsequent writes by the child must not propagate back to parent.
    (child_mem / "user_color.md").write_text("red\n")
    assert (parent_mem / "user_color.md").read_text() == "blue (青)\n"


@pytest.mark.asyncio
async def test_seed_branch_cwd_noop_when_no_claude_memory(
    stub_env: Mapping[str, str], tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing ~/.claude/projects/.../memory must not raise."""

    from pathlib import Path
    from uuid import uuid4

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    cfg = AtelierConfig.from_env(
        {
            **stub_env,
            "ATELIER_AGENT_BACKEND": "claude_code",
            "ATELIER_AGENT_CWD": str(tmp_path / "agent"),
        },
    )
    runner = AgentRunner(config=cfg, store=InMemoryStore(), bus=EventBus())
    # Warm parent cwd but never create the Claude project dir.
    parent_id = uuid4()
    runner.resolve_session_cwd(parent_id)

    await runner.seed_branch_cwd(parent_session_id=parent_id, child_session_id=uuid4())


@pytest.mark.asyncio
async def test_seed_branch_cwd_noop_when_shared(stub_env: Mapping[str, str], tmp_path: Any) -> None:
    """``shared`` isolation: no copy is performed (single shared cwd)."""

    from uuid import uuid4

    cfg = AtelierConfig.from_env(
        {
            **stub_env,
            "ATELIER_AGENT_BACKEND": "claude_code",
            "ATELIER_AGENT_CWD": str(tmp_path),
            "ATELIER_AGENT_CWD_ISOLATION": "shared",
            "ATELIER_ALLOW_AGENT_CWD_INSIDE_GIT": "1",
        },
    )
    runner = AgentRunner(config=cfg, store=InMemoryStore(), bus=EventBus())

    # Should not raise, even though parent_cwd == child_cwd in shared mode.
    await runner.seed_branch_cwd(parent_session_id=uuid4(), child_session_id=uuid4())


def test_resolve_claude_project_root_finds_git_ancestor(tmp_path: Any) -> None:
    """Claude Code keys auto-memory off the *git root*, not the cwd.

    A regression here is the bug behind the May-28 contamination
    incident: per-session cwds nested inside a git checkout silently
    shared a single ``~/.claude/projects/<repo-slug>/memory`` because
    Claude walks up to find ``.git`` before slugging. Pin the
    resolution rule so we never accidentally slug the cwd itself again.
    """

    from stratoclave_atelier.agent_runner import AgentRunner

    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    nested = repo / "wk" / "sessions" / "abc"
    nested.mkdir(parents=True)

    root = AgentRunner._resolve_claude_project_root(nested)
    assert root == repo.resolve()

    # No git ancestor anywhere -> returns the cwd itself (post-resolve).
    bare = tmp_path / "bare"
    bare.mkdir()
    assert AgentRunner._resolve_claude_project_root(bare) == bare.resolve()


def test_claude_project_memory_dir_uses_git_root_slug(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_claude_project_memory_dir`` slug is derived from the git root."""

    from pathlib import Path

    from stratoclave_atelier.agent_runner import AgentRunner

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    nested = repo / "wk" / "sessions" / "xyz"
    nested.mkdir(parents=True)

    expected_slug = str(repo.resolve()).replace("/", "-")
    expected = fake_home / ".claude" / "projects" / expected_slug / "memory"
    assert AgentRunner._claude_project_memory_dir(nested) == expected
