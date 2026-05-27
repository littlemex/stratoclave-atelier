"""Stage J AutoNamer: name a forked session from its parent context.

When a chat user clicks "Fork now" or "Branch from here", the new
session needs a human-readable title so the fork DAG sidebar can
display something more useful than ``branch-2026-05-27T09:30``. Stage J
introduces the :class:`AutoNamer` Protocol for that:

* :class:`LoomAutoNamer` runs a one-shot loom prompt over the most
  recent N turns and parses the response as the title.
* :class:`NoopAutoNamer` falls back to ``parent.title`` plus a 4-hex
  random suffix; used when no loom backend is available *or* when the
  loom call fails / times out / produces unusable output.

The orchestrator (``POST /api/sessions/{id}/branch``) always wraps the
auto-namer in a ``try/except`` and rotates to the noop on failure, so a
botched naming call never blocks the freeze + fork pipeline. The fork
title is determinstic-ish but not cached: a second fork from the same
turn intentionally produces a different suffix (or potentially a
different LLM-generated title), since each branch represents a distinct
intent.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
from typing import Any, Protocol

from stratoclave_atelier.config import AtelierConfig
from stratoclave_atelier.core.types import Event, Session

logger = logging.getLogger(__name__)

# Output sanitisation guards. The LoomAutoNamer prompt asks the model to
# return a short title only; we still defensively trim to keep the UI
# label-sized and reject obvious failures (empty / runaway).
_MAX_TITLE_LEN = 60
_MIN_TITLE_LEN = 2
_TITLE_TIMEOUT_SECONDS = 12.0
_PARENT_TURNS_TAIL = 6

_NAMING_PROMPT = (
    "You are naming a new branch in a chat session.\n"
    "Read the recent turns of the parent session below and output a SHORT title\n"
    "(3-6 words, max 60 characters) that describes the *intent* of branching off here.\n"
    "Examples: 'API design questions', 'slide draft', 'auth bugfix verification'.\n"
    "Output the title ONLY, no quotes, no preamble, no trailing punctuation.\n"
    "\n"
    "Recent turns:\n"
    "{turns}\n"
)


class AutoNamer(Protocol):
    """Pluggable strategy for naming a forked session."""

    @property
    def enabled(self) -> bool:  # pragma: no cover - trivial
        """``True`` when this implementation can produce LLM-derived titles.

        The orchestrator falls back to :class:`NoopAutoNamer` when this
        is ``False`` so the branch flow never blocks on a missing
        backend.
        """

    async def name_branch(
        self,
        *,
        parent: Session,
        recent_events: list[Event],
    ) -> str: ...


class NoopAutoNamer:
    """Deterministic fallback: ``<parent.title>-<4 hex>``.

    Used when no loom backend is configured, or when the LLM-backed
    namer fails / times out. A 4-hex suffix is enough to disambiguate
    siblings without bloating the chat header.
    """

    enabled: bool = False

    async def name_branch(
        self,
        *,
        parent: Session,
        recent_events: list[Event],
    ) -> str:
        del recent_events
        suffix = secrets.token_hex(2)  # 4 hex chars
        base = (parent.title or "branch").strip()
        candidate = f"{base}-{suffix}"
        return _clamp_title(candidate)


class LoomAutoNamer:
    """Run a one-shot loom prompt to name the branch.

    Uses the same backend resolution as :class:`AgentRunner` (Stage H)
    so the picker choice on the parent session also drives naming.
    Falls through to :class:`NoopAutoNamer` semantics when the loom
    response is empty / too long / errors out -- the orchestrator
    catches and substitutes, this class never has to.
    """

    enabled: bool = True

    def __init__(self, *, config: AtelierConfig) -> None:
        self._config = config

    async def name_branch(
        self,
        *,
        parent: Session,
        recent_events: list[Event],
    ) -> str:
        backend_name = self._resolve_backend(parent.agent_backend)
        cwd = self._config.cwd_for_backend(backend_name)
        if not cwd:
            raise RuntimeError(
                f"agent_cwd is not configured for backend {backend_name!r}; "
                "cannot run LoomAutoNamer"
            )
        prompt = _NAMING_PROMPT.format(turns=_format_turns(recent_events))
        text = await asyncio.wait_for(
            self._run_one_shot(backend_name=backend_name, cwd=cwd, prompt=prompt),
            timeout=_TITLE_TIMEOUT_SECONDS,
        )
        cleaned = _clean_loom_output(text)
        if len(cleaned) < _MIN_TITLE_LEN:
            raise RuntimeError(f"LoomAutoNamer produced empty/too-short title: {text!r}")
        return _clamp_title(cleaned)

    def _resolve_backend(self, requested: str | None) -> str:
        if requested:
            return requested
        if self._config.agent_backend != "none":
            return self._config.agent_backend
        allowed = self._config.resolved_backends()
        if len(allowed) == 1:
            return allowed[0]
        raise RuntimeError(
            "no default agent backend; set ATELIER_AGENT_BACKEND or pass parent.agent_backend"
        )

    async def _run_one_shot(self, *, backend_name: str, cwd: str, prompt: str) -> str:
        from stratoclave_loom import BackendConfig, create_session

        cfg = BackendConfig(
            backend=backend_name,
            cwd=cwd,
            allowed_tools=self._config.allowed_tools_for_backend(backend_name) or None,
        )
        session = create_session(cfg)
        try:
            stream = await session.send_message(prompt)
            buf: list[str] = []
            async for chunk in stream:
                if chunk.chunk_type == "text_delta":
                    text = str(chunk.content.get("text", ""))
                    if text:
                        buf.append(text)
                elif chunk.chunk_type == "end_turn":
                    break
                elif chunk.chunk_type == "error":
                    raise RuntimeError(f"loom returned error during naming: {chunk.content!r}")
            return "".join(buf)
        finally:
            try:
                await session.close()
            except Exception:  # pragma: no cover - defensive
                logger.exception("error closing one-shot loom session for naming")


def build_auto_namer(config: AtelierConfig) -> AutoNamer:
    """Pick an :class:`AutoNamer` based on whether a loom backend is wired.

    Mirrors :func:`stratoclave_atelier.memory.build_memory_service`: the
    server lifespan calls this once at startup. Tests can ignore this
    helper and inject a stub directly into ``app.state.auto_namer``.
    """

    if config.agent_backend == "none" and not config.resolved_backends():
        return NoopAutoNamer()
    return LoomAutoNamer(config=config)


def _format_turns(events: list[Event]) -> str:
    """Render the parent's recent turn payloads as a compact dialogue."""

    lines: list[str] = []
    tail = events[-_PARENT_TURNS_TAIL:] if len(events) > _PARENT_TURNS_TAIL else events
    for event in tail:
        if event.kind not in ("turn", "agent_turn"):
            continue
        payload: dict[str, Any] = dict(event.payload)
        role = str(payload.get("role") or "user")
        content = str(payload.get("content") or "")
        if not content:
            continue
        snippet = content.strip().replace("\r", "").replace("\n", " ")
        if len(snippet) > 280:
            snippet = snippet[:277] + "..."
        lines.append(f"{role}: {snippet}")
    if not lines:
        return "(no recent turns)"
    return "\n".join(lines)


def _clean_loom_output(text: str) -> str:
    """Strip markdown / quoting / trailing punctuation from the LLM output."""

    cleaned = text.strip()
    # Drop any code-fence wrapping the model may add.
    if cleaned.startswith("```") and cleaned.endswith("```"):
        cleaned = cleaned[3:-3].strip()
    # First line only -- some backends echo the question.
    if "\n" in cleaned:
        cleaned = cleaned.splitlines()[0].strip()
    # Drop wrapping quotes.
    cleaned = cleaned.strip("\"' ")
    # Drop a trailing period / exclamation, etc.
    cleaned = re.sub(r"[.!?\s]+$", "", cleaned)
    return cleaned


def _clamp_title(title: str) -> str:
    """Bound the final title to ``_MAX_TITLE_LEN`` chars."""

    if len(title) <= _MAX_TITLE_LEN:
        return title
    return title[: _MAX_TITLE_LEN - 1].rstrip() + "…"


__all__ = [
    "AutoNamer",
    "LoomAutoNamer",
    "NoopAutoNamer",
    "build_auto_namer",
]
