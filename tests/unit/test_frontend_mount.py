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


def test_memory_chip_shell_elements_are_present(client: TestClient) -> None:
    """Adopted-memory chip stays visible after Stage K @ panel removal."""

    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.text
    assert 'id="memory-chip"' in body
    assert 'id="memory-chip-clear"' in body


def test_mention_panel_is_removed(client: TestClient) -> None:
    """Stage L removes the @ session mention panel; the Curator replaces it."""

    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.text
    assert 'id="button-mention"' not in body
    assert 'id="mention-panel"' not in body
    assert 'id="mention-tab-distill"' not in body
    assert 'id="mention-results"' not in body

    js = client.get("/static/js/chat.js").text
    assert "openMentionPanel" not in js
    assert "runDistillSearch" not in js
    assert "runRawSearch" not in js
    # The memory chip helpers persist; the Curator can reuse them.
    assert "renderMemoryChip" in js


def test_curator_panel_shell_elements_are_present(client: TestClient) -> None:
    """Stage L: Curator dialog with scope summary, mode toggle, question, answer."""

    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.text
    assert 'id="curator-panel"' in body
    assert 'data-testid="curator-question"' in body
    assert 'data-testid="curator-answer"' in body
    assert 'data-testid="curator-mode-distill"' in body
    assert 'data-testid="curator-mode-raw"' in body
    assert 'data-testid="curator-scope-summary"' in body
    assert 'id="curator-ask"' in body
    assert "Ask the Curator" in body

    js = client.get("/static/js/chat.js").text
    assert "openCuratorPanel" in js
    assert "runCuratorQuery" in js
    assert "/api/curator/query" in js
    assert "Ask Curator about this session" in js


def test_chat_js_paints_group_color_via_inline_style(client: TestClient) -> None:
    """Group colour must use ``style.stroke`` to beat the chat.css rule.

    The DAG ``rect`` is targeted by ``.dag-node rect { stroke: var(--border) }``,
    which is a CSS rule. SVG ``setAttribute("stroke", ...)`` is a
    *presentation attribute* and loses to any matching CSS rule, so the
    group colour silently disappeared. The fix uses ``style.stroke``
    (inline style, higher specificity) so the colour actually paints.
    Pin the fix so a future "let's just use setAttribute" regression is
    caught at the unit-test layer.
    """

    resp = client.get("/static/js/chat.js")
    assert resp.status_code == 200
    body = resp.text
    assert "rect.style.stroke = grp.color" in body
    # The companion stroke-width inline style keeps the outline visible.
    assert 'rect.style.strokeWidth = "2.5px"' in body


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


def test_chat_js_loads_groups_before_rendering_dag(client: TestClient) -> None:
    """Hard-reload race: groups must hydrate before the first DAG paint.

    Otherwise ``state.groups`` is empty when ``renderDag`` runs, so
    ``state.groups.get(groupId)`` returns ``undefined`` and the rect
    outline silently falls back to the default ``var(--border)`` rule
    in chat.css. We pin two things:

    1. ``refreshGroupsOnly`` exists as a render-free helper that
       ``refreshForkGraph`` can await mid-flight.
    2. ``refreshForkGraph`` actually awaits it when the map is empty,
       which is the structural guarantee that the first paint has
       group colours available.
    """

    resp = client.get("/static/js/chat.js")
    assert resp.status_code == 200
    body = resp.text
    assert "async function refreshGroupsOnly()" in body
    assert "if (state.groups.size === 0)" in body
    assert "await refreshGroupsOnly()" in body


def test_chat_js_seeds_dag_from_full_session_list_on_reload(
    client: TestClient,
) -> None:
    """Hard reload must show every root + child, not just the active chain.

    Before this fix ``mergedGraph`` was an in-memory Map, so a hard
    refresh wiped it; ``refreshForkGraph`` then asked only the active
    session's group / ancestry, and every other root silently
    disappeared from the DAG. Pin the new behaviour: the renderer
    seeds ``mergedGraph`` from ``GET /api/sessions`` first, so all
    workspace roots survive the reload.
    """

    resp = client.get("/static/js/chat.js")
    assert resp.status_code == 200
    body = resp.text
    assert "async function hydrateAllSessionsIntoMergedGraph()" in body
    assert "await hydrateAllSessionsIntoMergedGraph()" in body
    # The hydration helper must talk to the workspace listing endpoint.
    assert 'api("GET", "/api/sessions")' in body


def test_chat_js_persists_session_across_reload(client: TestClient) -> None:
    """Hard reload must land on the same session the user was on.

    Two complementary mechanisms ship together:

    1. ``setActiveSession`` always syncs ``?session=<id>`` to the URL
       via ``replaceState`` (or ``pushState`` for explicit navigation),
       so the URL is the primary cursor.
    2. ``saveLastSessionId`` mirrors the id into ``localStorage`` under
       ``atelier:last-session-id`` so a bare ``/`` reload still recovers
       the session.

    The boot path consults the URL first and falls back to the
    localStorage cursor, then drives ``navigateToSession`` itself.
    """

    resp = client.get("/static/js/chat.js")
    assert resp.status_code == 200
    body = resp.text
    assert 'const LAST_SESSION_KEY = "atelier:last-session-id"' in body
    assert "function saveLastSessionId(sessionId)" in body
    assert "function loadLastSessionId()" in body
    assert "saveLastSessionId(session.session_id)" in body
    assert "window.history.replaceState(" in body
    assert "const stickySessionId = urlSessionId || loadLastSessionId()" in body


def test_chat_js_renders_solo_session_in_dag(client: TestClient) -> None:
    """A reloaded single-session workspace must still paint the DAG.

    Before this fix ``refreshForkGraph`` returned early when
    ``merged.nodes.length <= 1 && merged.edges.length === 0``, which
    made every freshly-reloaded solo session look "empty" -- the
    operator could not even see the node, let alone its group colour
    or right-click menu. The single-node hide branch is gone and the
    only remaining empty-state is "zero nodes".
    """

    resp = client.get("/static/js/chat.js")
    assert resp.status_code == 200
    body = resp.text
    assert "merged.nodes.length <= 1" not in body
    assert "single-session workspaces appear empty" in body
