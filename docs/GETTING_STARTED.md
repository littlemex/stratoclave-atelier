# stratoclave-atelier: Getting Started

**Last updated**: 2026-05-25
**Audience**: New contributors and operators bringing up atelier for the first time.

## Introduction

`stratoclave-atelier` is the UI + orchestration layer of the
**stratoclave** 4-OSS series. It records agent JSONL transcripts,
organizes them into groups, lets you fork from any past turn, and
freezes a fork as an immutable, content-addressed version that can be
referenced from other sessions.

This guide walks through what you need to get the **Stage A skeleton**
running on your laptop. Stage A is a runnable scaffold: FastAPI app,
five-table Postgres schema, docker-compose, and a `/healthz` endpoint.
Fork / freeze / group features land in Stage B onwards (see
`PROJECT_STATUS.md`).

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

Stage A only has unit tests under `tests/unit/`. The integration suite
under `tests/integration/` is wired up but empty until Stage B.

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
