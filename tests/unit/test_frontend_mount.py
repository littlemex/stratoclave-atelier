"""Test that Stage G chat is mounted at ``/`` and the panels SPA at ``/panels``."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from stratoclave_atelier.config import AtelierConfig
from stratoclave_atelier.db import InMemoryStore
from stratoclave_atelier.server import create_app


@pytest.fixture
def client(stub_config: AtelierConfig) -> Iterator[TestClient]:
    app = create_app(stub_config, store=InMemoryStore())
    with TestClient(app) as client:
        yield client


def test_root_serves_chat_index(client: TestClient) -> None:
    """Stage G chat ships at ``/``: minimal shell + chat.js + chat.css."""

    resp = client.get("/")
    assert resp.status_code == 200
    assert "stratoclave-atelier" in resp.text
    assert "/static/js/chat.js" in resp.text
    assert "/static/css/chat.css" in resp.text


def test_panels_serves_legacy_index(client: TestClient) -> None:
    """The Stage B-F four-panel UI is reachable at ``/panels``."""

    resp = client.get("/panels")
    assert resp.status_code == 200
    assert "/static/panels/js/app.js" in resp.text
    assert "/static/panels/css/app.css" in resp.text


def test_static_chat_js_is_served(client: TestClient) -> None:
    resp = client.get("/static/js/chat.js")
    assert resp.status_code == 200
    assert "agent-runs" in resp.text


def test_static_chat_css_is_served(client: TestClient) -> None:
    resp = client.get("/static/css/chat.css")
    assert resp.status_code == 200
    assert "chat-log" in resp.text


def test_static_panels_assets_are_served(client: TestClient) -> None:
    """Legacy panels assets continue to be served from ``/static/panels``."""

    js = client.get("/static/panels/js/app.js")
    assert js.status_code == 200
    assert "openIngestSocket" in js.text

    css = client.get("/static/panels/css/app.css")
    assert css.status_code == 200
    assert "graph-node" in css.text


def test_stage_j_shell_elements_are_present(client: TestClient) -> None:
    """Stage J: header has Fork now button, sidebar DAG, breadcrumb, dialogs."""

    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.text
    assert 'id="button-branch"' in body
    assert "Fork now" in body
    assert 'id="chat-breadcrumb"' in body
    assert 'id="chat-fork-dag"' in body
    assert 'id="dag-svg"' in body
    assert 'id="branch-confirm"' in body
    assert 'id="edge-memo"' in body


def test_stage_j_chat_js_carries_branch_logic(client: TestClient) -> None:
    """The chat module ships the orchestrator + DAG renderer."""

    resp = client.get("/static/js/chat.js")
    assert resp.status_code == 200
    body = resp.text
    assert "/branch" in body
    assert "fork-graph" in body
    assert "EDGE_MEMO_KEY" in body
    assert "layoutDag" in body


def test_chat_js_hydrate_renders_agent_turn(client: TestClient) -> None:
    """Hydration must consume both ``turn`` and ``agent_turn`` events.

    The previous version filtered to ``event === "turn"`` only, dropping
    historical assistant messages from the chat log. Live SSE then
    re-emitted them after every user turn, producing the
    ``user/user/assistant/assistant`` clustering bug after a fork. We
    pin the fix by asserting the source carries the dual-kind branch.
    """

    resp = client.get("/static/js/chat.js")
    assert resp.status_code == 200
    body = resp.text
    assert 'currentEvent === "turn" || currentEvent === "agent_turn"' in body


def test_chat_js_attaches_event_stream_with_resume_seq(client: TestClient) -> None:
    """The live tail must resume from ``lastSeenSeq + 1``.

    Without a resume cursor SSE replays from ``from_seq=0``, which causes
    every historical event to be re-emitted as if it were live -- the
    ordering bug surfaces immediately after a fork because hydration
    fills the log first, then SSE doubles every turn.
    """

    resp = client.get("/static/js/chat.js")
    assert resp.status_code == 200
    body = resp.text
    assert "attachEventStream(session.session_id, lastSeenSeq + 1)" in body
    assert "from_seq=${start}" in body
