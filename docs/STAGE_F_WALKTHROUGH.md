# Stratoclave Atelier -- Stage F Walkthrough

**Last updated**: 2026-05-26
**Scope**: Per-turn freeze UI, fork UI, snapshot-query UI, live-tail SSE,
HTTP turn-append fallback, CLI session subcommands.
**Status**: Done; merged once the `stage-f` PR lands on `main`.

This is the engineer-onboarding companion to `STAGE_D_E_WALKTHROUGH.md`.
Stages D and E delivered a 4-panel SPA that drove the
ingest -> freeze -> fork -> snapshot loop end-to-end, but the operator
surface was deliberately minimal: only "freeze whole session" was
exposed in the UI, and there was no terminal-friendly CLI for the same
operations. Stage F closes that gap.

---

## 1. The 30-second mental model

```
                   Stage F additions
        +---------------------------------------+
        | UI                                     |
        |   - per-turn "Freeze through here"     |
        |   - shift+click range freeze (anchor)  |
        |   - per-version "Fork" dialog          |
        |   - per-version "Snapshot query"       |
        |   - live-tail EventSource (SSE)        |
        |                                        |
        | API                                    |
        |   - POST /api/sessions/{id}/turns      |
        |     (HTTP fallback to WS ingest)       |
        |                                        |
        | CLI                                    |
        |   - stratoclave-atelier session ...    |
        |     list / show / send-turn /          |
        |     freeze / fork / snapshot-query     |
        |   - --in-memory no longer requires     |
        |     ATELIER_DATABASE_URL               |
        +---------------------------------------+
```

Stage F adds **no** schema changes, **no** new providers, and **no** new
core modules. The freeze / fork / snapshot-query primitives all already
existed in Stage C / D; Stage F is the operator-experience layer on top.

---

## 2. Per-turn freeze (range selector)

**Files**:
- `frontend/static/index.html` -- adds the `freeze-controls` div with
  range indicator + cancel button, plus `data-testid` hooks on every
  per-turn button.
- `frontend/static/css/app.css` -- `.turn-actions`, `.range-anchor`,
  `.range-indicator`, `.button-secondary`.
- `frontend/static/js/app.js::renderTimeline` -- emits two buttons per
  turn row (`Freeze through here`, `From here`).

### UX

Every turn row carries two small buttons:

- **Freeze through here**: POST `/api/sessions/{id}/freeze` with
  `{end_seq: seq, label: "freeze through #seq"}`. The server picks
  `start_seq=0` automatically, so this freezes turns `0..seq`
  inclusive.
- **From here**: toggles `state.rangeAnchorSeq` to the clicked turn's
  seq. The status row above the timeline shows
  `range start: turn #N` and a `Cancel range` button. While an anchor
  is set, clicking `Freeze through here` on a different turn freezes
  the inclusive range `[min(anchor,target), max(anchor,target)]` via
  `start_seq + end_seq` together.
- **Shift+click** on `Freeze through here` is an alias for `From here`,
  so a power user can do shift+click + click without taking their
  fingers off the timeline.

### Why two buttons instead of one

A single "Freeze..." popover could carry both flows, but it would force
the SPA to ship a popover library or a custom dropdown. Two flat
buttons keep the rendering pure DOM with no extra deps, and Playwright
journeys can target each flow with `data-testid="turn-freeze-through-button"`
or `data-testid="turn-range-anchor-button"`.

---

## 3. Fork UI (per-version dialog)

**Files**:
- `frontend/static/index.html` -- `<dialog id="dialog-fork">` element.
- `frontend/static/js/app.js::openForkDialog` /
  `submitForkDialog`.

### UX

Every Version row in the right panel now carries `Fork` and
`Snapshot query` buttons. Clicking `Fork` opens a native HTML
`<dialog>` element prefilled with:

- **Child session title** (free text, required).
- **fork_seq** (number input, defaulted to `version.start_seq`,
  bounded to `[start_seq, end_seq]` via `min` / `max`).

Submitting POSTs `/api/sessions/{parent_id}/fork` with
`{title, parent_version_id, fork_seq, group_id?}`, then calls
`selectSession(child)` so the operator lands on the new child session
immediately.

### Why a native `<dialog>`

`<dialog>` ships with `showModal()` / `close()` / focus trapping /
backdrop styling out of the box. We keep the SPA build-step-free,
which means no Headless UI / Radix / Material -- a native dialog is
the cheapest way to get accessible modal behavior.

---

## 4. Snapshot-query UI (per-version dialog)

**Files**:
- `frontend/static/index.html` -- `<dialog id="dialog-snapshot">` with
  a textarea + a `<pre id="snapshot-response">` output area.
- `frontend/static/js/app.js::openSnapshotDialog` /
  `submitSnapshotDialog`.

### UX

`Snapshot query` on a Version row opens a `<dialog>` with a
textarea. Submitting POSTs
`/api/sessions/{source_id}/snapshot-query` with
`{target_version_id, query}` and renders the resolver's `response`
field inline below the form (preformatted, max-height scrollable).

The default deployment ships `EchoSnapshotResolver` (Stage D), so the
walking skeleton produces a deterministic answer like
`[echo] version=v0-3 turns=4 ...`. Production replaces the resolver
via the `snapshot_resolver` kwarg to `create_app()`.

### Why no inline answer history

Each Version's snapshot-query history is already queryable via
`GET /api/snapshot-queries?target_version_id=...`. We deliberately
keep the dialog single-shot for now: the panel real-estate cost of an
inline history list outweighs the value when the resolver is still
just Echo.

---

## 5. Live-tail SSE in the SPA

**Files**:
- `frontend/static/js/app.js::openLiveTail` /
  `mergeIncomingEvent`.

### Wire-up

`selectSession()` now calls `openLiveTail()` after `loadTimeline()`.
`openLiveTail` opens an `EventSource` against
`/api/sessions/{id}/events?from_seq=N+1` where `N` is the highest seq
currently rendered. Every `message` event is parsed and merged into
`state.turns` via `mergeIncomingEvent`, which:

1. Returns immediately if the event is already rendered (dedupe by
   `event_id`).
2. Inserts and re-sorts by `seq`.
3. Updates `state.lastSeq`.
4. Calls `renderTimeline()` to repaint.

The WebSocket-driven `loadTimeline()` re-fetch from Stage E remains as
a backup so we don't lose ordering guarantees if the SSE stream drops.

### Why both SSE and WebSocket

WebSocket is the canonical *write* path: the SPA pushes turns through
it, and the server's `ack` frames give us flow control. SSE is the
canonical *read* path: it is simpler to consume from a browser, supports
`Last-Event-ID` resume, and survives proxy quirks better than long-held
WebSockets. Stage F uses both because each is the right tool for one
direction of the flow.

### Server-side caveat

The current FastAPI SSE handler (`api/events.py`) closes the stream
once it has replayed all events at or above `from_seq`. The SPA does
not yet retry on `error`; a Stage G refactor will switch the server
to keep the stream open and broadcast new events through an
`asyncio.Queue` per connection.

---

## 6. HTTP turn-append fallback

**Files**:
- `src/stratoclave_atelier/api/sessions.py::append_turn`
- `src/stratoclave_atelier/api/schemas.py::TurnAppend`

### Endpoint

```
POST /api/sessions/{session_id}/turns
Content-Type: application/json
{ "role": "user", "content": "hello" }
```

The handler:

1. Loads the session (404 on miss).
2. Refuses if `session.status != "active"` (409).
3. Calls `store.append_event(session_id, kind="turn", payload={"kind":"turn", "role":..., "content":...})`.
4. Returns the appended `EventRead` row (including `seq`).

### Why a fallback

The CLI lives in a separate process from the server, often over the
network. WebSocket ingest from the CLI would require maintaining a
keepalive socket, parsing ack frames, and handling reconnects -- all
of which is overkill for a one-shot `send-turn` invocation. The HTTP
endpoint reuses the same `append_event` primitive the WebSocket
handler does, so the persisted state is identical.

---

## 7. CLI session subcommands

**Files**:
- `src/stratoclave_atelier/cli.py` -- adds `session` parser and
  six subcommands; promotes `httpx` from dev to runtime dependency.
- `pyproject.toml` -- moves `httpx>=0.27` into runtime, adds
  `websockets>=12`.

### Surface

```
stratoclave-atelier session [--base-url URL] <command>

  list                 GET  /api/sessions[?group_id=...]
  show <session_id>    GET  /api/sessions/{id} + /versions
  send-turn <id>       POST /api/sessions/{id}/turns
                         --role <role> (default: user)
                         --content <text> (required)
  freeze <id>          POST /api/sessions/{id}/freeze
                         --start-seq N --end-seq N --label STR (all optional)
  fork <id>            POST /api/sessions/{id}/fork
                         --title STR (required)
                         --parent-version-id UUID (required)
                         --fork-seq N (required)
                         --group-id UUID (optional)
  snapshot-query <id>  POST /api/sessions/{id}/snapshot-query
                         --target-version-id UUID (required)
                         --query STR (required)
```

### Base URL resolution

```
args.base_url > $ATELIER_BASE_URL > http://localhost:8000
```

Any non-2xx response prints `error: METHOD PATH -> STATUS: detail` to
stderr and exits with code 2. 2xx responses print pretty-printed JSON
to stdout.

### Why HTTP-backed instead of in-process

A single CLI binary that opens a Postgres connection and calls Store
methods directly would couple the CLI tightly to the database driver
and force it to ship migration knowledge. An HTTP-backed CLI lets
operators target any running atelier instance (local, docker, prod
behind ALB) without changing the invocation, and keeps the surface
honest -- if the REST API does not expose an operation, the CLI cannot
do it either.

### `--in-memory` placeholder fix

`stratoclave-atelier serve --in-memory` previously crashed because
`AtelierConfig.from_env()` requires `ATELIER_DATABASE_URL`. The fix:

```python
if args.in_memory and not os.environ.get("ATELIER_DATABASE_URL"):
    os.environ["ATELIER_DATABASE_URL"] = _IN_MEMORY_PLACEHOLDER_URL
```

The placeholder is documented as a placeholder, not a real URL. The
in-memory backend never opens a connection, so the value is never
dialed. This keeps the no-hardcode policy intact -- a real database
URL is still required for any non-in-memory deployment.

---

## 8. Tests added in Stage F

```
tests/unit/
+-- test_api_sessions.py            +3 tests for POST /turns
|     - HTTP turn round-trip via TestClient
|     - 404 for unknown session
|     - 409 for frozen session
+-- test_cli.py                     +12 tests
      - --version short-circuit (existing)
      - bearer token redaction (existing)
      - new: session list / show / send-turn / freeze / fork /
        snapshot-query stub'd via httpx.Client monkeypatch
      - new: base URL via flag / env / default
      - new: error propagation -> exit code 2
      - new: serve --in-memory placeholder DB URL
```

Total unit suite at end of Stage F: **107 passing**, mypy strict clean
across all source files.

---

## 9. Manual demo flow

```bash
# Terminal A
stratoclave-atelier serve --in-memory --port 8123

# Terminal B
export ATELIER_BASE_URL=http://localhost:8123

# 1. Create a session
SID=$(curl -s -X POST :8123/api/sessions \
  -H 'content-type: application/json' \
  -d '{"title":"cli-demo"}' | jq -r .session_id)

# 2. Append a turn via CLI (HTTP path)
stratoclave-atelier session send-turn $SID \
  --role user --content "hello via CLI"

# 3. Freeze through turn 0
stratoclave-atelier session freeze $SID --end-seq 0 --label "after-hello"

# 4. Show the session
stratoclave-atelier session show $SID

# 5. Fork from the version (read VID from step 4 output)
VID=$(stratoclave-atelier session show $SID | jq -r '.versions[0].version_id')
stratoclave-atelier session fork $SID \
  --title "child-of-cli" --parent-version-id $VID --fork-seq 0

# 6. Snapshot-query the parent version
stratoclave-atelier session snapshot-query $SID \
  --target-version-id $VID --query "What did the user say?"
```

Open `http://localhost:8123/` in a browser to drive the same flows
through the SPA: each Version row now offers `Fork` and `Snapshot
query`; each turn row offers `Freeze through here` and `From here`.

---

## 10. What is intentionally not in Stage F

- **Auth on the CLI**. The session subcommands talk plain HTTP and
  honor whatever `ATELIER_AUTH_MODE` the server runs with -- if the
  server requires bearer auth, the CLI today has no way to send a
  token. A future stage adds an `--auth-token` flag that reads from
  `$ATELIER_BEARER_TOKEN`.
- **SSE keepalive on the server**. The SPA opens an `EventSource` but
  the server-side handler closes after replay; a future Stage G refactor
  switches to a long-lived broadcaster.
- **CLI live-tail**. There is no `session tail` subcommand yet; the
  HTTP `GET /events` endpoint is consumable with curl + line-mode
  parsing, so deferring it has minimal cost.
- **Production CSP / SRI**. Same as Stage E -- atelier sets these at
  deployment time, not in application code.

---

## 11. One-line summary

> Stage F made every flow that Stage D / E shipped in Python or curl
> reachable from a button or a CLI subcommand, without changing the
> data model.
