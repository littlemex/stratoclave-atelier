# Stratoclave Atelier — Stage D + E Walkthrough

**Last updated**: 2026-05-26
**Scope**: Fork-graph JSON, cross-session snapshot RPC, vanilla JS SPA, Playwright-driven E2E.
**Status**: Done; merged once the `stage-de` PR lands on `main`.

This is the engineer-onboarding companion to `STAGE_C_WALKTHROUGH.md`.
It explains the four moving parts that Stages D and E added — fork-graph
endpoints, the snapshot-query RPC, the static-mounted SPA, and the
walking-skeleton manual E2E — and where the corner cases live.

---

## 1. The 30-second mental model

```
                      Stage D (read paths)
        ┌─────────────────────────────────────────┐
        │ GET /api/groups/{id}/fork-graph         │
        │ GET /api/sessions/{id}/fork-graph       │
        │   -> { nodes: [...], edges: [...] }     │
        │                                         │
        │ POST /api/sessions/{id}/snapshot-query  │
        │   -> resolver.resolve(version, query)   │
        │   -> persist row in snapshot_queries    │
        └─────────────────────────────────────────┘

                      Stage E (UI)
        ┌─────────────────────────────────────────┐
        │ GET /                -> index.html      │
        │ GET /static/...      -> CSS / JS        │
        │ vanilla JS SPA wires REST + WS + SSE    │
        └─────────────────────────────────────────┘
```

* **Fork-graph endpoints are derived views.** They reuse `list_sessions`
  + `list_versions` + a pure helper (`fork_graph.build_fork_graph`) and
  add no new state. Stage D introduces no schema changes.
* **Snapshot-query is the only Stage D write path.** It validates the
  version exists, hands `(version, query, store, blob_store)` to a
  `SnapshotResolver`, and persists the resolver's response. The default
  `EchoSnapshotResolver` returns a deterministic string so the walking
  skeleton is reproducible without a live LLM.
* **The SPA is shipped as static files, not a build.** No webpack, no
  TypeScript, no React: vanilla ES modules, one HTML file, one CSS, one
  JS module. `frontend/static/` is mounted at `/static/`; `/` returns
  the index.

---

## 2. Fork-graph helper (`stratoclave_atelier.fork_graph`)

**File**: `src/stratoclave_atelier/fork_graph.py`

### Pure helper

```python
def build_fork_graph(
    sessions: Sequence[Session],
    versions: Sequence[Version],
) -> tuple[list[ForkGraphNode], list[ForkGraphEdge]]:
```

* Each input `Session` becomes one `ForkGraphNode`.
* Versions are bucketed by `session_id` and sorted by `end_seq` so the
  UI can render the per-session frozen-range list deterministically.
* An edge is emitted when `child.parent_session_id` and
  `child.parent_version_id` both reference items present in the inputs.
  This means the helper never invents edges that point at versions the
  caller has not explicitly fetched -- the graph stays consistent with
  whatever subset of the DAG you handed it.

### Why a pure function

Two endpoints hit this code: the group-scoped graph and the
session-scoped graph. Keeping the layout logic out of the API layer
means the unit test (`tests/unit/test_fork_graph.py`) covers the
interesting cases without standing up an HTTP client, and the same
helper can be reused later by a CLI or a static-export script.

---

## 3. Fork-graph endpoints (`stratoclave_atelier.api.fork_graph`)

**File**: `src/stratoclave_atelier/api/fork_graph.py`

### `GET /api/groups/{group_id}/fork-graph`

1. Validate the group exists -> 404 if not.
2. `list_sessions(group_id=...)` returns every session in the group.
3. For each session, `list_versions(session_id=...)` returns its frozen
   versions.
4. Pass the combined list to `build_fork_graph`.
5. Return `{nodes, edges}` Pydantic models.

### `GET /api/sessions/{session_id}/fork-graph`

This one is more interesting because a "session subgraph" is the
transitive closure of `parent_session_id` from a root.

1. Walk up `parent_session_id` to find the root of the fork tree (or
   stop at the first session whose parent is not in this group).
2. BFS down `parent_session_id` to collect every descendant.
3. Same `list_versions` per session, same helper call.

The BFS deliberately uses `list_sessions(group_id=root.group_id)` and
filters in-memory -- the dataset is small and we'd rather burn one
fetch + a Python set than maintain a recursive SQL CTE.

### Why no caching layer

The fork-graph response is small (one row per session, ~5 fields). On a
local machine the round trip is dominated by the SQL planner, and on a
prod deployment by network latency, neither of which a cache fixes.
Stage D defers caching until profiling justifies it.

---

## 4. Snapshot-query RPC (`stratoclave_atelier.api.snapshot_queries`)

**File**: `src/stratoclave_atelier/api/snapshot_queries.py`

### `POST /api/sessions/{session_id}/snapshot-query`

Request body:

```json
{ "target_version_id": "<uuid>", "query": "..." }
```

The handler:

1. Loads the source session (404 on miss).
2. Loads the target version (404 on miss).
3. Calls `snapshot_resolver.resolve(store=..., blob_store=...,
   version=..., query=...)`. The resolver is a Protocol; the default
   wiring instantiates `EchoSnapshotResolver`.
4. Calls `store.create_snapshot_query(...)` with the resolver's
   response, which returns a `SnapshotQuery` row.
5. Returns the row as `SnapshotQueryRead`.

### `GET /api/snapshot-queries`

Optional `source_session_id` and `target_version_id` query params filter
the returned list. Output is sorted descending by `created_at`.

### `EchoSnapshotResolver`

**File**: `src/stratoclave_atelier/snapshot_resolver.py`

```python
class EchoSnapshotResolver:
    async def resolve(self, *, store, blob_store, version, query):
        events = await store.list_events(version.session_id)
        turns = [e for e in events if e.kind == "turn"]
        return f"[echo] version={version.label or '?'} turns={len(turns)} ..."
```

It exists so the walking skeleton has a deterministic, testable path
end-to-end without depending on Bedrock / Claude / etc. Production
deployments swap in a real resolver via the `snapshot_resolver` kwarg
to `create_app()`.

### Why a Protocol instead of subclassing

The resolver gets the full `Store` + `BlobStore` plus the target
`Version` -- it has the freedom to read frozen JSONL, replay events,
embed a digest, and so on. Locking that surface to a single class would
force every integration to inherit, which doesn't compose well when
multiple integrations want to layer behavior. A Protocol lets `loom`
or `distill` ship their own resolver without depending on atelier's
class hierarchy.

---

## 5. The SPA (`frontend/static/`)

**Files**: `frontend/static/index.html`, `frontend/static/css/app.css`,
`frontend/static/js/app.js`.

### Why no build step

Atelier's UI is a four-panel diagnostic console: groups, sessions,
turn timeline, fork graph. No router, no state library, no SSR. A
single ES module beats the operational cost of webpack / vite / TS for
something this small. If the UI grows past one screen we can revisit;
until then, "view source" is the developer experience.

### Module layout

```
frontend/static/
├── index.html        4-panel grid + form fields with data-testid hooks
├── css/app.css       grid layout, panel cards, status toast, SVG styling
└── js/app.js         module-level `state` + helpers + event listeners
```

`index.html` carries the data-testid attributes that the Playwright
journey uses -- adding a new selector means adding one attribute, not
plumbing it through a component tree.

### Wire-up

* `loadGroups`, `loadSessions`, `loadVersions`, `loadForkGraph` -- pure
  REST GETs.
* `loadTimeline` -- consumes the SSE replay endpoint as a single HTTP
  fetch (not `EventSource`). Easier to test, and since the SPA does
  not currently keep a live tail open, an `EventSource` would just
  reconnect immediately.
* `openIngestSocket` -- one WebSocket per active session; closes the
  previous socket on session switch. Each ack from the server triggers
  `loadTimeline()` so the rendered list stays in sync with the event
  log.
* `sendTurn` -- payload shape is exactly the WebSocket frame the server
  expects (`{kind: "turn", role, content}`). The frame body is stored
  as `events.payload` directly, so any extra wrapping object would
  push the actual fields one level too deep.
* `renderForkGraph` -- deterministic SVG layout: depth = column,
  sibling index = row. Frozen sessions get the gold stroke via
  `.graph-node-rect.frozen` (CSS class set at render time).

### Static mount

**File**: `src/stratoclave_atelier/server.py::_mount_frontend`

```python
package_dir = Path(__file__).resolve().parent
repo_root = package_dir.parent.parent
static_dir = repo_root / "frontend" / "static"
if not static_dir.is_dir():
    return
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

@app.get("/", include_in_schema=False)
async def root() -> FileResponse:
    return FileResponse(str(static_dir / "index.html"))
```

If `frontend/static/` is missing (e.g. a wheel was installed without
the SPA assets) the mount is a no-op -- the API surface still works.

### `--in-memory` CLI flag

**File**: `src/stratoclave_atelier/cli.py`

```bash
stratoclave-atelier serve --in-memory --port 8123
```

Builds the app with `InMemoryStore` + `InMemoryBlobStore` so no
Postgres / disk is required. Used by the Playwright walking-skeleton
demo and the CI E2E job. Note that we pass the constructed app
instance directly to `uvicorn.run` (not the import-string form) so the
in-memory state survives across requests within a single process.

---

## 6. Manual Playwright walking-skeleton flow

The Stage E E2E test journey is run by hand while the architecture is
still settling. Replaying it locally:

```bash
stratoclave-atelier serve --in-memory --port 8123 &
# Open http://localhost:8123/ in a browser (or drive Playwright)
```

Steps verified end-to-end on 2026-05-26:

1. **Create a group** "demo" -> appears in the Groups list.
2. **Create a session** "root-session" inside the group -> appears in
   the Sessions list, fork-graph renders one node ("active · no
   versions").
3. **Send a turn** as `user` with content `hello world` -> Turns list
   shows `#0 user: hello world`.
4. **Click "Freeze whole session"** -> Versions list gains
   `frozen at <iso>` and the fork-graph node flips to gold stroke
   ("active · 1 version").
5. **Snapshot-query RPC** (via curl or fetch):
   ```bash
   SID=$(curl -s :8123/api/sessions | jq -r '.[0].session_id')
   VID=$(curl -s :8123/api/sessions/$SID/versions | jq -r '.[0].version_id')
   curl -s -X POST :8123/api/sessions/$SID/snapshot-query \
     -H 'content-type: application/json' \
     -d "{\"target_version_id\":\"$VID\",\"query\":\"What did the user say?\"}"
   ```
   Returns a row whose `response` matches the `EchoSnapshotResolver`
   format.

The screenshot in `docs/assets/stage_e_walking_skeleton.png` captures
step 4.

---

## 7. Where the new tests live

```
tests/unit/
├── test_fork_graph.py             Stage D-2 (pure helper)
├── test_api_fork_graph.py         Stage D-3 (REST level, in-memory)
├── test_api_snapshot_queries.py   Stage D-3 (REST level, RecordingResolver stub)
├── test_snapshot_resolver.py      Stage D (Echo resolver)
└── test_frontend_mount.py         Stage E (StaticFiles + index.html)
```

The full unit suite (93 tests) plus the existing integration tests pass
locally with `pytest -q`. Mypy strict mode is clean across all 49 source
files.

---

## 8. What's intentionally not in Stage D / E

* **Live LLM resolver**. The walking skeleton uses Echo. Wiring a real
  Bedrock / Anthropic resolver lands with the cross-repo integration
  in a later stage.
* **Auth on the SPA**. The SPA assumes no auth at the moment; Bearer
  token / Cognito wiring is a later stage gate. The Playwright flow
  uses `ATELIER_AUTH_MODE=none` (the default).
* **Live SSE tail in the SPA**. `loadTimeline` is a single fetch +
  WebSocket-driven re-fetch. A push-driven tail would be additive.
* **Fork-graph caching**. See section 3.
* **Per-turn freeze button**. The SPA only offers "freeze whole
  session" -- the per-range API is exposed in Stage C but the UI
  surface for it (shift+click on a turn row) is deferred.
* **Production CSP headers and SRI hashes**. The SPA loads its own
  files only, but a production deployment will want a hardened
  Content-Security-Policy. Atelier sets this at deployment time, not
  in the application code.

---

## 9. One-line summary

> Stage D made fork lineages and frozen versions queryable end-to-end;
> Stage E shipped a 4-panel vanilla-JS console that drives the whole
> ingest -> freeze -> snapshot loop without a build step.
