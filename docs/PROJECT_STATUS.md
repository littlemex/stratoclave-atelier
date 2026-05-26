# stratoclave-atelier: Implementation Status

**Last updated**: 2026-05-26
**Project started**: 2026-05-25

## Overall progress

### Stage completion

| Stage | Scope | Status |
|-------|-------|--------|
| A     | Runnable skeleton: FastAPI app + 5-table schema + docker-compose + `/healthz` | Done |
| B     | Store layer (Protocol + InMemory + asyncpg), groups + sessions + versions REST | Done |
| C     | Content-addressed BlobStore, WebSocket ingest, freeze (whole + range), SSE replay | Done |
| D     | Cross-session snapshot RPC, fork DAG JSON, distill / loom integration | Not started |
| E     | UI (fork DAG visualization + per-turn freeze buttons) | Not started |

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

### What ships in Stage C (this delta)

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
| Backend | (lead)   | Active   | Stage D: fork-graph JSON + cross-session snapshot RPC |
| UI      | -        | Pending  | Awaits Stage D handoff         |

## Next steps (priority order)

1. **Begin Stage D**: `GET /api/groups/{id}/fork-graph` and
   `GET /api/sessions/{id}/fork-graph` returning DAG JSON for the UI.
2. **Cross-session snapshot RPC**:
   `POST /api/sessions/{id}/snapshot-query` that resolves a frozen
   version + question and logs the result to `snapshot_queries`.
3. **stratoclave-distill integration**: optional digest pre-fill on
   freeze.
4. **stratoclave-loom integration**: optional "spawn an agent on this
   version" runtime hook.
