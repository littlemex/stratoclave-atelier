# stratoclave-atelier: Getting Started

**Last updated**: 2026-05-25
**Audience**: New contributors and operators bringing up atelier for the first time.

## Introduction

`stratoclave-atelier` is the UI + orchestration layer of the
**stratoclave** 4-OSS series. It records agent JSONL transcripts,
organizes them into groups, lets you fork from any past turn, and
freezes a fork as an immutable, content-addressed version that can be
referenced from other sessions.

This guide walks through what you need to get atelier running on your
laptop. Stage A shipped a runnable scaffold (FastAPI app, five-table
Postgres schema, docker-compose, `/healthz`); **Stage B** adds the
`Store` Protocol with in-memory and asyncpg implementations and exposes
the first batch of REST endpoints under `/api/groups` and
`/api/sessions` (including fork). Freeze + JSONL ingest land in Stage C
(see `PROJECT_STATUS.md`).

## Where atelier sits in the 4-OSS series

```
stratoclave            -- auth + Bedrock proxy
stratoclave-loom       -- agent backend abstraction
stratoclave-distill    -- distillation + group rollup + hybrid search
stratoclave-atelier    -- UI + DB + orchestrator (this repo)
```

You do **not** need any of the other repos to bring up Stage A locally.
Atelier is self-contained; the integration points to loom and distill
appear in later stages.

## Prerequisites

- **Python 3.11 or 3.12**
- **Docker / finch / podman** for the local Postgres + pgvector container
- A POSIX-ish shell (macOS or Linux); Windows users should run from WSL

Optional but recommended:

- `uv` for faster installs (`pip install uv` and substitute `uv pip` below)

## Set up

```bash
git clone https://github.com/littlemex/stratoclave-atelier.git
cd stratoclave-atelier

python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

This installs the runtime dependencies (FastAPI, SQLAlchemy[asyncio],
asyncpg, alembic, pgvector, pydantic) plus the dev extras (pytest,
ruff, mypy, httpx).

## Bring up Postgres

```bash
docker compose up -d
docker compose ps
```

You should see one service (`postgres`) with a healthcheck status of
`healthy` after a few seconds. The pgvector image already provides the
`vector` extension; the first migration enables it explicitly.

## Run migrations

```bash
export DATABASE_URL=postgresql+psycopg://atelier:atelier@localhost:5432/atelier
alembic upgrade head
```

This creates the five atelier tables: `groups`, `sessions`,
`versions`, `events`, `snapshot_queries`. See
`migrations/versions/0001_initial_schema.py` for the schema.

## Start the server

```bash
export ATELIER_DATABASE_URL=postgresql+asyncpg://atelier:atelier@localhost:5432/atelier
stratoclave-atelier serve
```

Or directly with uvicorn:

```bash
uvicorn stratoclave_atelier.server:app --reload
```

## Smoke check

```bash
curl -s localhost:8000/healthz
# => {"status":"ok"}
```

## REST API (Stage B)

Stage B exposes the first slice of the REST surface. All requests
return JSON; errors come back as `{"detail": "..."}` with the standard
HTTP status (404 for missing entities, 409 for invariant violations).

```bash
# Create a group.
curl -s -X POST localhost:8000/api/groups \
  -H 'content-type: application/json' \
  -d '{"name": "ops", "description": null}'

# Create a root session inside that group.
curl -s -X POST localhost:8000/api/sessions \
  -H 'content-type: application/json' \
  -d '{"title": "incident-2026-05-25", "group_id": "<group_id>"}'

# Fork a child session from a frozen version of the parent at turn 3.
curl -s -X POST localhost:8000/api/sessions/<parent_id>/fork \
  -H 'content-type: application/json' \
  -d '{"title": "branch-A", "parent_version_id": "<version_id>", "fork_seq": 3}'

# List the frozen versions belonging to a session.
curl -s localhost:8000/api/sessions/<session_id>/versions
```

Versions are not yet writable through HTTP -- Stage C adds the freeze
endpoint backed by the content-addressed JSONL blob store. For now the
only way to land Versions is to insert them directly via the
`AsyncpgStore` (e.g. from a script or integration test).

## Configuration

All knobs are environment variables. Nothing is hard-coded in `src/`.

| Variable                  | Default                           | Purpose                                                |
|---------------------------|-----------------------------------|--------------------------------------------------------|
| `ATELIER_DATABASE_URL`    | (required)                        | asyncpg URL the FastAPI app uses                       |
| `DATABASE_URL`            | (required for migrations)         | psycopg URL alembic uses                               |
| `ATELIER_HOST`            | `0.0.0.0`                         | uvicorn bind host                                      |
| `ATELIER_PORT`            | `8000`                            | uvicorn bind port                                      |
| `ATELIER_LOG_LEVEL`       | `info`                            | uvicorn log level                                      |
| `ATELIER_AUTH_MODE`       | `none`                            | `none` / `bearer` / `stratoclave_cognito`              |
| `ATELIER_BEARER_TOKEN`    | (unset)                           | Required when `ATELIER_AUTH_MODE=bearer`               |
| `ATELIER_BLOB_DIR`        | `.atelier-blobs`                  | Where frozen JSONL blobs are written                   |

To preview the effective configuration:

```bash
stratoclave-atelier config
```

## Run tests

```bash
pytest
```

Stage B ships unit tests for the in-memory store, the REST handlers,
the CLI, the config edge cases, and the `/healthz` endpoint. The
integration suite under `tests/integration/` exercises the alembic
migration and the asyncpg store against a live Postgres; both are
gated on `ATELIER_TEST_DATABASE_URL` and skipped without it.

```bash
# Run integration tests locally (requires `docker compose up -d`).
export ATELIER_TEST_DATABASE_URL=postgresql+asyncpg://atelier:atelier@localhost:5432/atelier
pytest -m integration tests/integration
```

To run a quick lint + type check before pushing:

```bash
ruff check .
ruff format --check .
mypy src/stratoclave_atelier
```

## Troubleshooting

- **`ATELIER_DATABASE_URL is required`**: export the env var before running
  `stratoclave-atelier serve`.
- **`bearer_token is required when auth_mode='bearer'`**: also set
  `ATELIER_BEARER_TOKEN`.
- **`alembic upgrade head` fails with `extension "vector" is not available`**:
  you are not using the `pgvector/pgvector:pg16` image; check
  `docker-compose.yml`.
- **uvicorn cannot bind**: another process is on port 8000. Override
  with `ATELIER_PORT=8080 stratoclave-atelier serve`.

## What's next

- Read `PROJECT_STATUS.md` for the live roadmap.
- Read `PROJECT_RULES.md` before opening your first PR.
- Stage B will introduce the in-memory `Store` + asyncpg `Store`, the
  groups / sessions / versions REST endpoints, and the JSONL ingest
  WebSocket.
