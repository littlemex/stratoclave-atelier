# stratoclave-atelier: Implementation Status

**Last updated**: 2026-05-27 (Stage G)
**Project started**: 2026-05-25

## Overall progress

### Stage completion

| Stage | Scope | Status |
|-------|-------|--------|
| A     | Runnable skeleton: FastAPI app + 5-table schema + docker-compose + `/healthz` | Done |
| B     | Store layer (Protocol + InMemory + asyncpg), groups + sessions + versions REST | Done |
| C     | Content-addressed BlobStore, WebSocket ingest, freeze (whole + range), SSE replay | Done |
| D     | Fork-graph JSON + cross-session snapshot RPC + Echo resolver | Done |
| E     | Vanilla JS SPA (4 panels) + static mount + `--in-memory` CLI + walking-skeleton E2E | Done |
| F     | Per-turn freeze UI + fork dialog + snapshot-query dialog + live-tail SSE + HTTP turn fallback + CLI session subcommands | Done |
| G     | Real agent loop via stratoclave-loom + cross-session memory via stratoclave-distill + claude-capture-style chat at `/` + legacy panels moved to `/panels` | Done |

### What ships in Stage A

| Component | File(s) | State |
|-----------|---------|-------|
| FastAPI app factory | `src/stratoclave_atelier/server.py` | Done |
| `/healthz` endpoint | `src/stratoclave_atelier/api/health.py` | Done |
| Config (env-driven, no hardcoding) | `src/stratoclave_atelier/config.py` | Done |
| Domain types (Group / Session / Version) | `src/stratoclave_atelier/core/types.py` | Done |
| Error hierarchy | `src/stratoclave_atelier/core/errors.py` | Done |
| CLI (`serve`, `migrate`, `config`) | `src/stratoclave_atelier/cli.py` | Done |
| Alembic 0001: 5-table schema | `migrations/versions/0001_initial_schema.py` | Done |
| docker-compose: Postgres + pgvector | `docker-compose.yml` | Done |
| 3 mandatory docs | `docs/{GETTING_STARTED,PROJECT_STATUS,PROJECT_RULES}.md` | Done |
| CI workflow | `.github/workflows/ci.yml` | Done |
| Unit tests | `tests/unit/` | Done |

### What ships in Stage G (this delta)

| Component | File(s) | State |
|-----------|---------|-------|
| `AgentRunner` (one loom `AgentSession` per atelier session, streaming + persistence + cancellation) | `src/stratoclave_atelier/agent_runner.py` | Done |
| `EventBus` (asyncio.Queue pub-sub for SSE live broadcast + backpressure resync) | `src/stratoclave_atelier/events_bus.py` | Done |
| `POST /api/sessions/{id}/agent-runs` (202 + fire-and-forget, SSE drives the response) and `/cancel` (204) | `src/stratoclave_atelier/api/agent_runs.py` | Done |
| SSE endpoint switched to live broadcast (`follow=true`) with 15 s `: ping` keepalive and replay-then-tail dedupe | `src/stratoclave_atelier/api/events.py` | Done |
| `MemoryService` Protocol + `NoopMemoryService` + async `build_memory_service` | `src/stratoclave_atelier/memory.py` | Done |
| `DistillMemoryService` (lazy-imported, owns asyncpg pool + IngestRunner + Retriever) | `src/stratoclave_atelier/_distill_memory.py` | Done |
| `freeze_session` calls `memory.ingest_session(events=selected)` post-Version | `src/stratoclave_atelier/freeze.py` | Done |
| `MemoryServiceDep` DI alias + freeze handler wiring | `src/stratoclave_atelier/api/deps.py`, `api/sessions.py` | Done |
| FastAPI lifespan: `await build_memory_service(cfg)` + `await memory.aclose()` | `src/stratoclave_atelier/server.py` | Done |
| Stage G chat at `/` (vanilla ES module, claude-capture style) | `frontend/static/index.html`, `frontend/static/js/chat.js`, `frontend/static/css/chat.css` | Done |
| Stage B-F 4-panel SPA moved to `/panels` (URL preserved for power users) | `frontend/static/panels/` | Done |
| Config knobs: `ATELIER_AGENT_BACKEND`, `ATELIER_AGENT_CWD`, `ATELIER_AGENT_ALLOWED_TOOLS`, `ATELIER_AGENT_MEMORY`, `ATELIER_DISTILL_ENABLED`, `ATELIER_DISTILL_DATABASE_URL` | `src/stratoclave_atelier/config.py` | Done |
| `[memory]` optional extra: `stratoclave-distill` + `asyncpg` | `pyproject.toml` | Done |
| Unit tests (Stage G adds `test_agent_runner.py`, `test_api_agent_runs.py`, `test_events_bus.py`, `test_memory.py`; expands `test_api_events.py`, `test_frontend_mount.py`) | `tests/unit/` | Done |
| Walkthrough doc | `docs/STAGE_G_WALKTHROUGH.md` | Done |

### What ships in Stage F

| Component | File(s) | State |
|-----------|---------|-------|
| Per-turn freeze UI (`Freeze through` button + shift-click range anchor) | `frontend/static/index.html`, `frontend/static/css/app.css`, `frontend/static/js/app.js` | Done |
| Fork dialog (`<dialog id="dialog-fork">` + `submitForkDialog`) | `frontend/static/index.html`, `frontend/static/js/app.js` | Done |
| Snapshot-query dialog (`<dialog id="dialog-snapshot">` + `submitSnapshotDialog`) | `frontend/static/index.html`, `frontend/static/js/app.js` | Done |
| Live-tail SSE (`EventSource` + `mergeIncomingEvent`) | `frontend/static/js/app.js::openLiveTail` | Done |
| `POST /api/sessions/{id}/turns` (HTTP turn append, fallback to WS ingest) | `src/stratoclave_atelier/api/sessions.py::append_turn`, `api/schemas.py::TurnAppend` | Done |
| `--in-memory` placeholder DB URL so `serve --in-memory` runs without `ATELIER_DATABASE_URL` | `src/stratoclave_atelier/cli.py::_cmd_serve` | Done |
| CLI `session` subcommands (`list` / `show` / `send-turn` / `freeze` / `fork` / `snapshot-query`) | `src/stratoclave_atelier/cli.py` | Done |
| `httpx` and `websockets` promoted to runtime deps | `pyproject.toml` | Done |
| Unit tests (CLI subcommands + HTTP turn append + frozen-session 409) | `tests/unit/test_cli.py`, `tests/unit/test_api_sessions.py` | Done |
| Walkthrough doc | `docs/STAGE_F_WALKTHROUGH.md` | Done |

### What ships in Stages D + E (Stage D + E delta)

| Component | File(s) | State |
|-----------|---------|-------|
| `fork_graph.build_fork_graph` pure helper | `src/stratoclave_atelier/fork_graph.py` | Done |
| `GET /api/groups/{id}/fork-graph` and `GET /api/sessions/{id}/fork-graph` | `src/stratoclave_atelier/api/fork_graph.py` | Done |
| `SnapshotResolver` Protocol + `EchoSnapshotResolver` (default wiring) | `src/stratoclave_atelier/snapshot_resolver.py` | Done |
| `POST /api/sessions/{id}/snapshot-query` and `GET /api/snapshot-queries` | `src/stratoclave_atelier/api/snapshot_queries.py` | Done |
| `InMemoryStore.create_snapshot_query` / `list_snapshot_queries` | `src/stratoclave_atelier/db/memory.py` | Done |
| `AsyncpgStore.create_snapshot_query` / `list_snapshot_queries` (raw SQL via asyncpg) | `src/stratoclave_atelier/db/asyncpg_store.py` | Done |
| Pydantic schemas: `ForkGraph*Read`, `SnapshotQueryCreate`, `SnapshotQueryRead` | `src/stratoclave_atelier/api/schemas.py` | Done |
| Vanilla JS SPA (4-panel: groups / sessions / turns / fork graph) | `frontend/static/index.html`, `frontend/static/css/app.css`, `frontend/static/js/app.js` | Done |
| Static mount (`/static/*`) + index handler (`/`) | `src/stratoclave_atelier/server.py::_mount_frontend` | Done |
| `--in-memory` CLI flag for walking-skeleton demo | `src/stratoclave_atelier/cli.py` | Done |
| Unit tests (5 new files): fork-graph helper + REST + snapshot-query + Echo + frontend mount | `tests/unit/test_fork_graph.py`, `test_api_fork_graph.py`, `test_api_snapshot_queries.py`, `test_snapshot_resolver.py`, `test_frontend_mount.py` | Done |
| Walkthrough doc + walking-skeleton screenshot | `docs/STAGE_D_E_WALKTHROUGH.md`, `docs/assets/stage_e_walking_skeleton.png` | Done |

### What ships in Stage C

| Component | File(s) | State |
|-----------|---------|-------|
| `BlobStore` Protocol + `FileBlobStore` (write-once, `chmod 0444`, sha256 fan-out) + `InMemoryBlobStore` | `src/stratoclave_atelier/blobs/store.py` | Done |
| Freeze pipeline (events -> JSONL -> BlobStore -> Version) | `src/stratoclave_atelier/freeze.py` | Done |
| `POST /api/sessions/{id}/freeze` (whole session or `start_seq..end_seq` range) | `src/stratoclave_atelier/api/sessions.py` | Done |
| `WS /api/sessions/{id}/ingest` (one JSONL turn per text frame, ack stream) | `src/stratoclave_atelier/api/ingest.py` | Done |
| `GET /api/sessions/{id}/events?from_seq=N` (SSE replay) | `src/stratoclave_atelier/api/events.py` | Done |
| FastAPI lifespan: build `FileBlobStore` rooted at `cfg.blob_dir` | `src/stratoclave_atelier/server.py` | Done |
| `BlobStoreDep` DI alias | `src/stratoclave_atelier/api/deps.py` | Done |
| `SessionFreeze` request schema | `src/stratoclave_atelier/api/schemas.py` | Done |
| Unit tests: `BlobStore`, freeze pipeline, freeze REST, ingest WS, SSE replay (32 new tests) | `tests/unit/test_blob_store.py`, `test_freeze.py`, `test_api_freeze.py`, `test_api_ingest.py`, `test_api_events.py` | Done |
| Integration test: full freeze pipeline against live Postgres + filesystem | `tests/integration/test_asyncpg_store.py` | Done |

### What ships in Stage B

| Component | File(s) | State |
|-----------|---------|-------|
| `Store` Protocol | `src/stratoclave_atelier/db/store.py` | Done |
| `InMemoryStore` (test backend) | `src/stratoclave_atelier/db/memory.py` | Done |
| `AsyncpgStore` (runtime backend, raw SQL via SQLAlchemy async) | `src/stratoclave_atelier/db/asyncpg_store.py` | Done |
| Domain `Event` type + `EventKind` literal | `src/stratoclave_atelier/core/types.py` | Done |
| `ConflictError` for invariant violations | `src/stratoclave_atelier/core/errors.py` | Done |
| FastAPI lifespan: build engine + AsyncpgStore on startup, dispose on shutdown | `src/stratoclave_atelier/server.py` | Done |
| Pydantic 2 request/response schemas | `src/stratoclave_atelier/api/schemas.py` | Done |
| `StoreDep` DI alias + 404 / 409 helpers | `src/stratoclave_atelier/api/deps.py` | Done |
| `POST/GET /api/groups`, `GET /api/groups/{id}` | `src/stratoclave_atelier/api/groups.py` | Done |
| `POST/GET /api/sessions`, `GET /api/sessions/{id}`, `POST /api/sessions/{id}/fork`, `GET /api/sessions/{id}/versions` | `src/stratoclave_atelier/api/sessions.py` | Done |
| Unit tests for `InMemoryStore` (14 tests) | `tests/unit/test_memory_store.py` | Done |
| Unit tests for `/api/groups` + `/api/sessions` | `tests/unit/test_api_groups.py`, `tests/unit/test_api_sessions.py` | Done |
| Integration tests for `AsyncpgStore` (gated on `ATELIER_TEST_DATABASE_URL`) | `tests/integration/test_asyncpg_store.py` | Done |
| CI: integration job against pgvector service container | `.github/workflows/ci.yml` | Done |

### Database schema

| Table              | Purpose |
|--------------------|---------|
| `groups`           | Containers for related sessions |
| `sessions`         | Individual agent conversations; `parent_version_id` + `fork_seq` form the fork DAG |
| `versions`         | Immutable, content-addressed JSONL snapshots (full or turn range); SHA-256 of bytes |
| `events`           | Per-session monotonic event log (drives SSE history) |
| `snapshot_queries` | Audit log of cross-session RPC snapshot lookups |

## What's intentionally out of scope (forever)

These were excluded by explicit project decision (see `HANDOVER.md`):

- **Memo features.** Atelier focuses on fork / freeze / group; memo
  features stay outside the scope.
- **Remote machine connection.** Local-host orchestration only.
- **claude-capture migration.** Atelier is a completely new project,
  not a rewrite. claude-capture stays as-is.
- **claude-capture API back-compat.** Atelier defines its own REST
  surface; we will not maintain compatibility shims.

## Roadmap (Stage B onwards)

### Stage B -- Store + REST CRUD

- Add `Store` Protocol (read / write surface for the 5 tables).
- `InMemoryStore` for unit tests; `AsyncpgStore` for the runtime.
- Wire dependency injection through FastAPI's `app.state`.
- Endpoints:
  - `POST /api/groups`, `GET /api/groups`, `GET /api/groups/{id}`
  - `POST /api/sessions`, `GET /api/sessions`, `GET /api/sessions/{id}`
  - `POST /api/sessions/{id}/fork` (fork from a frozen version + start_seq)
  - `GET /api/sessions/{id}/versions`
- Integration tests against the docker-compose Postgres.

### Stage C -- Ingest + freeze + blobs

- Content-addressed blob store under `ATELIER_BLOB_DIR` (write-once,
  `chmod 0444` after rename).
- WebSocket ingest endpoint that appends turns to a session's event log
  and persists the JSONL line to a temporary file.
- `POST /api/sessions/{id}/freeze` (freeze whole session).
- `POST /api/sessions/{id}/versions/{vid}/freeze` -- per-turn / range
  freeze with optional `start_seq`.
- SSE `GET /api/sessions/{id}/events?from_seq=N` for replay.

### Stage D -- Fork DAG + cross-session RPC + integration

- `GET /api/groups/{id}/fork-graph` and `GET /api/sessions/{id}/fork-graph`
  returning DAG JSON.
- Cross-session `POST /api/sessions/{id}/snapshot-query` that resolves
  a frozen version + question into a synchronous answer (logged into
  `snapshot_queries`).
- Optional integration with stratoclave-distill for digest pre-fill on
  freeze.
- Optional integration with stratoclave-loom for "start an agent on
  this version" runtime spawning.

### Stage E -- UI

- Fork DAG visualization (D3 / vis-network / Cytoscape; <200KB target).
- Per-turn freeze buttons (1-click for "freeze from this turn through
  the end"; shift+click for explicit ranges).
- Group / session list, session detail, and JSONL viewer.

## Team / ownership

| Role    | Owner    | Status   | Current task                   |
|---------|----------|----------|--------------------------------|
| Backend | (lead)   | Active   | Stage G shipped: AgentRunner + SSE live broadcast + MemoryService; push pending |
| UI      | (lead)   | Active   | Stage G shipped: chat at `/`, panels at `/panels`; auth still pending |

## Next steps (priority order)

1. **`DistillSnapshotResolver`**: replace `EchoSnapshotResolver` with a
   real distill-backed resolver so snapshot-query can answer semantic
   questions (currently echoes the question back).
2. **CLI `session tail`**: subscribe to the SSE stream and stream JSON
   events to stdout, mirroring the SPA's live tail.
3. **Loom "spawn on version"**: add a button on each frozen Version row
   to start a new chat seeded with that JSONL.
4. **Auth wiring**: bearer token / Cognito mode end-to-end through the
   SPA (currently relies on `ATELIER_AUTH_MODE=none`).
5. **Memory ingestion observability**: surface failed
   `memory.ingest_session` attempts in the panels UI (currently
   logged-only).
