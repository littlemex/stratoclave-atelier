# Stage J Walkthrough: Branch from chat (auto-named) + side DAG + edge memos

**Last updated**: 2026-05-27

Stage J turns the panels-only fork/freeze plumbing into a first-class
chat affordance. The goal, from the user's own framing:

> A single chat session quickly becomes a junk drawer when you mix
> "design questions" with "implementation work" with "make slides from
> these notes". Branching is the answer -- spin off a side conversation
> for each intent, keep the main thread clean, and let me jump between
> them without leaving the chat shell.

Stage J ships the orchestration that makes one-click branching usable
without leaving `/`:

1. **`POST /api/sessions/{id}/branch`** -- a single-call orchestrator
   that freezes the parent's turn range, asks an `AutoNamer` for a
   short title, and forks a new session whose `parent_version_id` /
   `fork_seq` point at the freshly frozen Version. The handler is
   deliberately defensive: any `AutoNamer` failure rotates to a
   deterministic `parent.title-<4 hex>` suffix so a flaky LLM never
   blocks the branch flow.
2. **`AutoNamer` Protocol** -- pluggable strategy with two built-ins:
   `LoomAutoNamer` (one-shot loom prompt over the recent N turns) and
   `NoopAutoNamer` (deterministic `<parent>-<hex>` fallback). The
   server lifespan picks one via `build_auto_namer(cfg)`.
3. **Chat shell upgrades** -- a `Fork now` button in the header, a
   per-turn `Branch from here` hover affordance, a breadcrumb showing
   the ancestry chain, a right-side SVG DAG with clickable nodes for
   navigation, and an edge-memo dialog whose contents persist in
   `localStorage` (key `atelier:fork-edge-memos`).

Together the three pieces close the most-asked-for chat ergonomics gap
since Stage G's claude-capture-style shell landed: branching is now a
two-click operation that keeps the user in `/` while the audit trail
(Versions + fork DAG) stays consistent with the panels.

## What changed

### Source layout

| Area | Files |
|------|-------|
| AutoNamer | `src/stratoclave_atelier/auto_namer.py` (new: `AutoNamer` Protocol, `LoomAutoNamer`, `NoopAutoNamer`, `build_auto_namer`) |
| DI | `src/stratoclave_atelier/api/deps.py::get_auto_namer` + `AutoNamerDep` alias |
| Wiring | `src/stratoclave_atelier/server.py::lifespan` (build via `build_auto_namer(cfg)` unless an explicit instance is injected) |
| Schemas | `src/stratoclave_atelier/api/schemas.py` (new: `SessionBranch`, `SessionBranchResponse`) |
| Endpoint | `src/stratoclave_atelier/api/sessions.py::branch_session` |
| Frontend shell | `frontend/static/index.html`, `frontend/static/css/chat.css` |
| Frontend logic | `frontend/static/js/chat.js` (state extension, breadcrumb, DAG renderer, dialogs, history sync) |
| Tests | `tests/unit/test_auto_namer.py` (14 tests), `tests/unit/test_api_branch.py` (8 tests), additions to `tests/unit/test_frontend_mount.py` (Stage J shell + JS markers) |

No new migrations, no new Python deps -- `LoomAutoNamer` reuses the
Stage G `stratoclave-loom` extra and the orchestrator only composes
existing primitives (`freeze_session`, `Store.create_session`).

### Config knobs

Stage J introduces no new env vars. `AutoNamer` selection follows the
same backend resolution as the agent loop:

* When `ATELIER_AGENT_BACKEND=none` and `ATELIER_AGENT_BACKENDS_ALLOWED`
  is empty, `build_auto_namer(cfg)` returns `NoopAutoNamer` so titles
  are always `<parent>-<4 hex>`.
* Otherwise it returns `LoomAutoNamer(config=cfg)`. The same
  `agent_cwd` / `allowed_tools` resolution as `AgentRunner` applies.

This means a deployment that already runs Stage G/H with a real loom
backend gets LLM-named branches "for free" the moment it picks up
Stage J; deployments running with `none` keep deterministic suffixes.

## `POST /api/sessions/{id}/branch`

### Request

```jsonc
{
  "title": null,           // optional; when set, AutoNamer is skipped
  "start_seq": null,       // optional; defaults to 0 (whole session)
  "end_seq": null,         // optional; defaults to last seq (whole session)
  "label": null,           // optional Version label
  "group_id": null,        // optional; inherits parent.group_id when omitted
  "agent_backend": null    // optional; inherits parent.agent_backend when omitted
}
```

`start_seq` / `end_seq` are forwarded straight through to
`freeze_session`. The child session's `fork_seq` is set to
`payload.end_seq` when the caller pinned the range, otherwise to the
freshly minted Version's `end_seq` -- so "branch from this turn"
flows (UI button on a single turn) and "branch from now" flows
(header button) both produce the right child cursor.

### Response (`201 Created`)

```jsonc
{
  "child": { /* SessionRead */ },
  "parent_version": { /* VersionRead -- the freshly frozen snapshot */ },
  "auto_named": true     // false when the caller pinned title or the namer fell back
}
```

`auto_named` is the UI's signal for whether to highlight the title as
"machine-generated, double-check me" vs. "this is what you typed".
The chat shell uses it to colour-code the breadcrumb crumb that
appears for the new session.

### Error contract

| Status | Reason |
|--------|--------|
| 404 | Parent session id not found |
| 409 | Empty session (no turns yet -> nothing to freeze), backend not in allowed list, or freeze invariant violation |
| 422 | Pydantic validation of `SessionBranch` (e.g. negative `start_seq`) |

The only "happy path" status is 201. The handler intentionally never
5xxs: a misbehaving `AutoNamer` is caught and substituted, freeze
errors map to 404/409 via the existing `http_not_found` / `http_conflict`
helpers, and backend validation reuses the Stage H `_resolve_backend`
path.

## `AutoNamer`

### Protocol

```python
class AutoNamer(Protocol):
    @property
    def enabled(self) -> bool: ...

    async def name_branch(
        self,
        *,
        parent: Session,
        recent_events: list[Event],
    ) -> str: ...
```

The `enabled` flag tells the orchestrator whether the namer is
"genuinely producing titles" or just acting as a deterministic
fallback. The `auto_named` field of the response mirrors this -- the
UI uses it to differentiate "LLM picked the title" from "we made one
up because the LLM is offline".

### LoomAutoNamer

* Resolves the backend the same way `AgentRunner` does: parent
  session's `agent_backend` overrides config; otherwise falls back to
  `cfg.agent_backend` or, when only one backend is allowed, that one.
* Builds a one-shot prompt out of the parent's last 6 turns
  (`_PARENT_TURNS_TAIL`), formatted as `role: snippet` lines with each
  snippet trimmed to 280 chars.
* Calls `stratoclave_loom.create_session(BackendConfig(...))`,
  iterates the chunk stream until `end_turn`, and joins the
  `text_delta` payloads.
* Sanitises the LLM output: strip code fences, take only the first
  line, strip wrapping quotes, drop trailing punctuation.
* Wraps the entire round-trip in `asyncio.wait_for(..., timeout=12.0)`
  so a hung loom backend cannot block the branch.
* Raises on empty / too-short output (`<2 chars`) so the orchestrator
  can rotate to the noop.

### NoopAutoNamer

* Returns `f"{parent.title or 'branch'}-{secrets.token_hex(2)}"`.
* `enabled = False`, so the response always reports `auto_named: false`.
* Used both as the explicit fallback and as the orchestrator's
  exception-handler substitute -- so even when `LoomAutoNamer` is the
  configured strategy, a single failure cleanly downgrades to a
  deterministic suffix without changing the contract.

### Output guarantees

| Guard | Where |
|-------|-------|
| Title <= 60 chars | `_clamp_title` (truncates to 59 + ellipsis) |
| Title >= 2 chars | `_clean_loom_output` raises `RuntimeError` otherwise |
| Single line, no surrounding quotes / fences / punctuation | `_clean_loom_output` |
| Branch never blocks on namer failure | orchestrator catches `Exception` and rotates to `NoopAutoNamer.name_branch` |

## Chat shell additions

### Header

The chat header gains one new button next to the existing controls:

```html
<button id="button-branch" data-testid="chat-branch">Fork now</button>
```

Click the button -> `<dialog id="branch-confirm">` opens with the
current seq prefilled (read-only) plus an optional title override
input. Submit hits `POST /api/sessions/{id}/branch` and, on success,
calls the central `setActiveSession(child, {pushHistory: true})`
dispatcher so the URL, breadcrumb, and DAG all stay in sync.

### Per-turn hover affordance

Each `user` / `assistant` message renders a `Branch from here` button
positioned with `position: absolute; right: 0; top: 0` and hidden
unless the user hovers the message. Clicking opens the same
`branch-confirm` dialog, but with the turn's seq prefilled so the
freeze stops at that exact point.

### Breadcrumb

```html
<nav id="chat-breadcrumb" class="chat-breadcrumb"></nav>
```

`renderBreadcrumb(session)` walks the `parent_session_id` chain via
the `sessionsCache: Map<UUID, SessionRead>` (lazily filled on demand)
and renders one clickable crumb per ancestor + the current session.
Click any crumb -> `navigateToSession(id)`. The current crumb is
styled as inert text; ancestors render as buttons with
`aria-current="false"`.

### Right-side DAG

```html
<aside id="chat-fork-dag" class="chat-fork-dag">
  <header class="dag-header">Forks</header>
  <div id="dag-empty" class="dag-empty">No forks yet</div>
  <svg id="dag-svg" viewBox="..."></svg>
</aside>
```

* `refreshForkGraph()` fetches `/api/groups/{group_id}/fork-graph`
  when the active session has a group; otherwise it builds a synthetic
  graph by walking the ancestry chain via `sessionsCache`.
* `layoutDag(graph)` runs a deterministic topological levelisation:
  roots (no parent in the graph) go on row 0, children go to
  `max(parent.row) + 1`. Each row's nodes are spaced uniformly across
  the SVG width.
* `renderDag(graph)` draws one `<rect>` + `<text>` per node and one
  `<path d="M...">` per edge. The active session's rectangle gets
  `.dag-node.current` so it stands out.
* Each node `<rect>` carries a `data-session-id` attribute and a
  click handler -> `navigateToSession(...)`.
* Each edge `<path>` carries `data-parent-id` / `data-child-id` and
  opens the edge-memo dialog on click. The label rendered next to the
  edge is `edgeMemos["{parent}->{child}"]` when set, otherwise the
  edge's `fork_seq` (e.g. `seq=42`).

### Edge memos (localStorage)

```js
const EDGE_MEMO_KEY = "atelier:fork-edge-memos";
// shape: { "<parent_uuid>-><child_uuid>": "slide draft for design review" }
```

* `loadEdgeMemos()` reads + JSON-parses on boot; returns `{}` when the
  key is missing or invalid.
* `persistEdgeMemos()` writes back via `localStorage.setItem`.
* `openEdgeMemoDialog(parentId, childId)` prefills the textarea from
  the existing memo (if any). `saveEdgeMemo()` writes the value back
  and re-renders the DAG so the new label appears immediately.
* This is intentionally not server-persisted: per the user's framing
  ("これは重要情報ではないので local storage に入れてもいい"), edge
  memos are scratchpad notes, not audit trail.

### URL sync

`setActiveSession()` calls `window.history.pushState({sessionId}, "",
"?session=<uuid>")` so each branch / navigation lands on a deep-link
that survives a tab restore. A `popstate` listener restores the
previous session from `event.state.sessionId` so the browser's back
button moves between branches as expected.

When `?session=<id>` is present on first boot, the chat hydrates the
session via `GET /api/sessions/{id}` + `GET /api/sessions/{id}/events?follow=false`.
The hydrate path reuses the SSE replay parser (`event:` / `data:`
line pairs) so historical turns render exactly as they would after a
live tail.

### Dialogs

| Dialog | Purpose | Submit action |
|--------|---------|---------------|
| `<dialog id="branch-confirm">` | Confirm the seq + optional title before forking | `POST /api/sessions/{id}/branch` -> `navigateToSession(child.session_id)` |
| `<dialog id="edge-memo">` | Edit the localStorage memo for an edge | Update `edgeMemos`, persist, re-render DAG |

Both use the native `<dialog>` element so the browser handles focus
trap + backdrop + Esc-to-close without extra JS.

## Tests

### `tests/unit/test_auto_namer.py` (14 tests)

* `_format_turns` -- empty / many turns / non-turn events / long content trim / role default.
* `_clean_loom_output` -- strip code fence / trailing punctuation / quotes / take first line.
* `_clamp_title` -- short pass-through / truncate to 59 + ellipsis.
* `NoopAutoNamer` -- format `<parent>-<4 hex>` / fallback "branch" base.
* `LoomAutoNamer` -- happy path / timeout / empty output rejection / explicit backend override / loom error chunk.

### `tests/unit/test_api_branch.py` (8 tests)

* freezes_and_forks (default whole-session range, AutoNamer called, `auto_named: true`)
* explicit_title_skips_namer (caller-pinned title -> AutoNamer never called, `auto_named: false`)
* range_uses_supplied_seqs (`start_seq` / `end_seq` round-trip, `fork_seq == end_seq`)
* falls_back_to_noop_on_namer_failure (RuntimeError -> child title starts with `parent.title-`, `auto_named: false`)
* unknown_session_returns_404 / empty_session_returns_409
* inherits_parent_backend / explicit_backend_override

### `tests/unit/test_frontend_mount.py` (+2 tests)

* `test_stage_j_shell_elements_are_present` -- `button-branch`, "Fork now", `chat-breadcrumb`, `chat-fork-dag`, `dag-svg`, `branch-confirm`, `edge-memo` all in `/`.
* `test_stage_j_chat_js_carries_branch_logic` -- `/branch`, `fork-graph`, `EDGE_MEMO_KEY`, `layoutDag` all in chat.js.

## Verification

```bash
ruff check src tests
ruff format --check src tests
mypy src/
pytest -q tests/unit
```

Stage J final state:

* 197 unit tests (was 173 in Stage I; +24 = 14 namer + 8 branch + 2 frontend).
* mypy strict over 33 source files.
* ruff clean.

## Operating with branching

```bash
# Stage J needs no new env vars. Bring up the chat the same way as
# Stage G/H:
export ATELIER_DATABASE_URL="postgresql+asyncpg://atelier:atelier@localhost:5432/atelier"
export ATELIER_AGENT_BACKEND=claude_code
export ATELIER_AGENT_CWD=$PWD

stratoclave-atelier serve --port 8123
# open http://localhost:8123/
```

Then:

1. Type a few prompts to build up some context in the parent session.
2. Click "Fork now" in the header -> dialog confirms the seq -> hit
   "Branch". The new session takes over the chat pane; the breadcrumb
   shows `parent > <auto-named child>`; the right-side DAG shows two
   nodes connected by an edge labelled with the fork seq.
3. Click the edge in the DAG -> memo dialog opens. Type
   "slide draft for design review" -> Save. The edge label updates
   instantly.
4. Click the parent crumb in the breadcrumb -> chat hydrates the
   parent session's history. Click "Fork now" again to spin off a
   second sibling. The DAG now shows three nodes (parent + two
   children).

## What's still on the punch list

`PROJECT_STATUS.md::Next steps` after Stage J:

1. Auth wiring (bearer / Cognito) end-to-end through the chat shell.
2. Memory ingestion observability in the panels UI.
3. Optional LLM-backed snapshot resolver (Stage I extension).
4. Server-side persistence of edge memos (currently localStorage only;
   acceptable per Stage J framing but a future stage could promote
   them to a dedicated table for cross-device sync).
