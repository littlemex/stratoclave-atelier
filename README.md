# stratoclave-atelier

> Workshop for agent sessions: fork, freeze, and group conversations.

`stratoclave-atelier` is the UI + orchestration layer of the **stratoclave**
4-OSS series. It is a self-contained Docker stack that records agent JSONL
sessions, organizes them into groups, lets you fork from any past turn, and
freezes a fork as an immutable, content-addressed version that can be
referenced from other sessions.

```
stratoclave            -- auth + Bedrock proxy
stratoclave-loom       -- agent backend abstraction (claude-code, kiro-code, ...)
stratoclave-distill    -- session distillation + group rollup + hybrid search
stratoclave-atelier    -- UI + DB + orchestrator (this repo)
```

## Status

**v0.1 (Stage A) -- bootstrap skeleton.** FastAPI app + Postgres schema +
docker-compose. No fork / freeze / group features yet; this commit only
establishes the runnable scaffold (`/healthz` returns 200) and the database
shape we plan to fill in across Stage B-D.

See `docs/PROJECT_STATUS.md` for the live roadmap and what is/isn't done.

## What it is for

- **Fork** any session from any prior turn, producing a new sibling
  conversation that shares history up to the fork point.
- **Freeze** a session (or a turn range) into an immutable version with a
  stable id, so the version can be referenced as snapshot context from any
  other session.
- **Group** related sessions so distilled learnings and group rollups (from
  `stratoclave-distill`) hang off the right scope.
- Visualize the fork DAG of a session group in the UI (planned for v0.2).
- Per-turn freeze buttons in the UI so you can freeze "from this turn" with
  one click (planned for v0.2; backend endpoint already designed).

Out of scope on purpose: memo features, remote machine connection,
claude-capture data migration, and back-compat with claude-capture's API.
This repo is a **completely new project**, not a rewrite.

## Quick start

```bash
# 1. Bring up Postgres + pgvector
docker compose up -d

# 2. Install
pip install -e ".[dev]"

# 3. Run migrations
DATABASE_URL=postgresql+psycopg://atelier:atelier@localhost:5432/atelier \
  alembic upgrade head

# 4. Start the API
ATELIER_DATABASE_URL=postgresql+asyncpg://atelier:atelier@localhost:5432/atelier \
  uvicorn stratoclave_atelier.server:app --reload

# 5. Smoke check
curl -s localhost:8000/healthz
# => {"status":"ok"}
```

## Configuration

All knobs are environment variables. Nothing is hard-coded in `src/`.

| Variable | Default | Purpose |
|---|---|---|
| `ATELIER_DATABASE_URL` | (required) | asyncpg URL the FastAPI app uses |
| `DATABASE_URL` | (required for migrations) | psycopg URL alembic uses |
| `ATELIER_AUTH_MODE` | `none` | `none` / `bearer` / `stratoclave_cognito` |
| `ATELIER_BEARER_TOKEN` | (unset) | Required when `ATELIER_AUTH_MODE=bearer` |
| `ATELIER_HOST` | `0.0.0.0` | uvicorn bind host |
| `ATELIER_PORT` | `8000` | uvicorn bind port |
| `ATELIER_LOG_LEVEL` | `info` | uvicorn log level |

## License

Apache-2.0. See `LICENSE`.
