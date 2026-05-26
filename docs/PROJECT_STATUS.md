# stratoclave-atelier: Implementation Status

**Last updated**: 2026-05-25
**Project started**: 2026-05-25

## Overall progress

### Stage completion

| Stage | Scope | Status |
|-------|-------|--------|
| A     | Runnable skeleton: FastAPI app + 5-table schema + docker-compose + `/healthz` | Done |
| B     | Store layer (Protocol + InMemory + asyncpg), groups + sessions + versions REST | Done |
| C     | JSONL ingest (WebSocket + SSE), per-turn freeze button backend, blob store | Not started |
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

### What ships in Stage B (this delta)

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
| Backend | (lead)   | Active   | Stage C: ingest + freeze + blob store |
| UI      | -        | Pending  | Awaits Stage D handoff         |

## Next steps (priority order)

1. **Begin Stage C**: content-addressed blob store under
   `ATELIER_BLOB_DIR`, write-once with `chmod 0444` after rename.
2. **WebSocket ingest** that appends turns to a session's event log and
   spools the JSONL to a temp file.
3. **Freeze endpoints**: `POST /api/sessions/{id}/freeze` (whole) and
   per-turn / range freeze backed by the blob store.
4. **SSE replay**: `GET /api/sessions/{id}/events?from_seq=N`.
