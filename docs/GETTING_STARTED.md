# stratoclave-atelier: Getting Started

**Last updated**: 2026-05-27 (Stage H)
**Audience**: New contributors and operators bringing up atelier for the first time.

## Introduction

`stratoclave-atelier` is the UI + orchestration layer of the
**stratoclave** 4-OSS series. It records agent JSONL transcripts,
organizes them into groups, lets you fork from any past turn, and
freezes a fork as an immutable, content-addressed version that can be
referenced from other sessions.

This guide walks through what you need to get atelier running on your
laptop. Stages A through H are merged: Postgres schema and CRUD
(A / B), WebSocket ingest plus content-addressed freeze (C), fork-graph
JSON and cross-session snapshot RPC (D), a vanilla-JS SPA that drives
the whole loop (E), per-turn freeze + fork dialog + snapshot-query
dialog + live-tail SSE + HTTP turn fallback + a `session` family of
CLI subcommands (F), a real agent loop via stratoclave-loom +
cross-session memory via stratoclave-distill + claude-capture-style
chat at `/` with the legacy 4-panel SPA preserved at `/panels` (G), and
per-session backend selection in the chat header so operators can pick
claude_code / kiro_code / mock per session (H). See
`PROJECT_STATUS.md` for the up-to-date component matrix and
`STAGE_H_WALKTHROUGH.md` for the latest walkthrough.

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
asyncpg, alembic, pgvector, pydantic, httpx, websockets) plus the dev
extras (pytest, ruff, mypy). Stage F promoted `httpx` and `websockets`
to runtime deps because the CLI's `session` subcommands talk to the
running server over HTTP and the WS ingest endpoint requires the
`websockets` package at import time.

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

# Stage F: append a single turn over plain HTTP, mirroring the
# WebSocket ingest path. Useful when curl / scripts cannot easily
# negotiate a WebSocket handshake.
curl -s -X POST localhost:8000/api/sessions/<session_id>/turns \
  -H 'content-type: application/json' \
  -d '{"role": "user", "content": "hello"}'
```

Versions are written through `POST /api/sessions/{id}/freeze` (Stage
C) and the WebSocket at `/api/sessions/{id}/ingest` is the canonical
path for appending turns. Stage D adds two more shapes:

```bash
# Group-level fork DAG (nodes + edges JSON for the UI).
curl -s localhost:8000/api/groups/<group_id>/fork-graph

# Cross-session snapshot RPC: resolve a frozen version + question into
# a synchronous answer (logged to snapshot_queries).
curl -s -X POST localhost:8000/api/sessions/<source_session_id>/snapshot-query \
  -H 'content-type: application/json' \
  -d '{"target_version_id": "<version_id>", "query": "What did the user say?"}'
```

The default deployment ships `EchoSnapshotResolver` so the walking
skeleton is reproducible without an LLM. Production replaces it via
the `snapshot_resolver` kwarg to `create_app()`.

## Stage E: vanilla JS SPA

The `frontend/static/` SPA is mounted at `/` and drives the whole
ingest -> freeze -> snapshot loop end-to-end. To bring up the walking
skeleton without Postgres, use the `--in-memory` flag:

```bash
stratoclave-atelier serve --in-memory --port 8123
# then open http://localhost:8123/ in a browser
```

The four panels are: groups, sessions filtered by the active group,
turns + versions for the active session, and the SVG fork graph. See
`docs/STAGE_D_E_WALKTHROUGH.md` for the manual Playwright journey.

## Stage F: SPA upgrades

Stage F adds three interactive surfaces to the SPA:

- **Per-turn freeze**. Each turn row gets a `Freeze through` button.
  A plain click freezes from that turn's `seq` through the latest
  `seq`. Shift+click on two turns marks an explicit
  `start_seq..end_seq` range (the first sets the anchor, the second
  closes the range and triggers the freeze).
- **Fork dialog**. Each Version row gets a `Fork` button. The dialog
  asks for a child title and a `fork_seq` clamped to the version's
  `start_seq..end_seq`, then calls `POST /api/sessions/{id}/fork`.
- **Snapshot query**. Each Version row also gets a `Snapshot query`
  button. The dialog asks a question, posts to
  `POST /api/sessions/{id}/snapshot-query`, and renders the response
  in-line.

A live-tail `EventSource` against `/api/sessions/{id}/events` keeps the
timeline current without manual reloads, and the turn form falls back
to `POST /api/sessions/{id}/turns` if the WebSocket is unavailable.

## Stage F: CLI session subcommands

The `stratoclave-atelier session` family talks to the running server
over HTTP. It is a thin admin shim, not a replacement for the SPA.

```bash
# Where the CLI looks for the server (priority: --base-url > env > default).
export ATELIER_BASE_URL=http://localhost:8123

# List sessions, optionally scoped to a group.
stratoclave-atelier session list
stratoclave-atelier session list --group-id <group_id>

# Show one session and its versions.
stratoclave-atelier session show <session_id>

# Append a single turn (HTTP fallback to WS ingest).
stratoclave-atelier session send-turn <session_id> --role user --content "hi"

# Freeze the whole session (or pass --start-seq / --end-seq for a range).
stratoclave-atelier session freeze <session_id> --label baseline

# Fork a child session from a frozen version.
stratoclave-atelier session fork <parent_session_id> \
  --title branch-A --parent-version-id <version_id> --fork-seq 3

# Run the cross-session snapshot RPC against a Version.
stratoclave-atelier session snapshot-query <source_session_id> \
  --target-version-id <version_id> --query "what changed?"
```

Every subcommand emits the response body as pretty-printed JSON on
stdout. Failures (HTTP >= 400) print `error: METHOD path -> status: detail`
to stderr and exit with status 2.

## Stage G: chat at `/` and panels at `/panels`

Stage G changes the front door. Browsing to `/` now shows a single-pane
chat: a textarea, a stream of `user` / `assistant` bubbles, and a
"Freeze" button. The four-panel UI from Stages B-F still exists --
it has just moved to `/panels`. Both surfaces share the same SSE
event stream, so a freeze triggered from chat shows up in the panels'
Versions list and vice versa.

To bring up a chat with a real agent locally:

```bash
# Pick a backend that loom knows about. claude_code is the default
# happy path; kiro_code also works.
export ATELIER_AGENT_BACKEND=claude_code
export ATELIER_AGENT_CWD=$PWD

stratoclave-atelier serve --in-memory --port 8123
# open http://localhost:8123/ in a browser, type a prompt, hit Enter
```

`ATELIER_AGENT_BACKEND=none` (the default) makes the chat boot into a
read-only mode: posting a prompt returns `503 Service Unavailable` so
operators can decide when to wire a real backend.

## Stage G: cross-session memory (optional)

Memory is opt-in and runs on top of `stratoclave-distill`. Install the
extra and switch the knobs:

```bash
pip install -e ".[memory]"

export ATELIER_DISTILL_ENABLED=true
export ATELIER_DISTILL_DATABASE_URL=postgresql://distill:distill@localhost:5432/distill
export ATELIER_AGENT_MEMORY=true   # default

stratoclave-atelier serve
```

Once memory is enabled:

- `POST /api/sessions/{id}/freeze` hands the selected turn events to
  distill so it can extract canonical / emerging / conflict / gap
  rows. Failures are logged and never block the freeze.
- `POST /api/sessions/{id}/agent-runs` calls
  `Retriever.retrieve(query=prompt)` and prepends the result as a
  `<memory>...</memory>` block before the user prompt. The chat marks
  these turns with a `memory: on` badge.

Either knob set to `false` (or the optional extra missing) demotes
atelier to `NoopMemoryService`: the boot still succeeds, prompts go
through unmodified, and the chat does not show the memory badge.

## Stage H: per-session backend selection (optional)

Stage H lets one atelier deployment offer multiple loom backends from
the chat header. Configure the allowed list and (optionally)
per-backend cwd / allowed_tools:

```bash
export ATELIER_AGENT_BACKENDS_ALLOWED="claude_code,kiro_code,mock"
export ATELIER_AGENT_BACKEND=claude_code        # default if picker is untouched
export ATELIER_AGENT_CWD="$PWD/.atelier-wk"     # shared default

# Per-backend overrides (optional)
export ATELIER_AGENT_CWD_KIRO_CODE="$PWD/.atelier-kc"
export ATELIER_AGENT_ALLOWED_TOOLS_CLAUDE_CODE="shell.run,file.read"

stratoclave-atelier serve
```

Open `http://localhost:8000/`. The Backend dropdown lists the allowed
entries; pick one and the next `POST /api/sessions` ships
`agent_backend` so the choice is persisted on the session row. The
dropdown locks once a session is warm to prevent mid-stream engine
swaps; click "New session" to unlock it again. Forks inherit the
parent session's backend by default.

When `ATELIER_AGENT_BACKENDS_ALLOWED` is empty the Stage G singular
behaviour applies: `ATELIER_AGENT_BACKEND` is the only allowed entry.

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
| `ATELIER_BASE_URL`        | `http://localhost:8000`           | Base URL the CLI `session` subcommands target          |
| `ATELIER_AGENT_BACKEND`   | `none`                            | Default loom backend: `none` / `claude_code` / `kiro_code` / `mock` |
| `ATELIER_AGENT_BACKENDS_ALLOWED` | (unset)                    | Stage H: CSV of backends offered by the chat picker    |
| `ATELIER_AGENT_CWD`       | (unset)                           | Working dir for backends without per-backend override (required when backend != `none`) |
| `ATELIER_AGENT_CWD_<BACKEND>` | (unset)                       | Stage H: per-backend cwd override (e.g. `ATELIER_AGENT_CWD_KIRO_CODE`) |
| `ATELIER_AGENT_ALLOWED_TOOLS` | (unset)                       | Comma-separated allowlist passed through to loom       |
| `ATELIER_AGENT_ALLOWED_TOOLS_<BACKEND>` | (unset)             | Stage H: per-backend allowed_tools override            |
| `ATELIER_AGENT_MEMORY`    | `true`                            | Per-server toggle for memory retrieval on agent runs   |
| `ATELIER_DISTILL_ENABLED` | `false`                           | Wire `DistillMemoryService` (requires the `[memory]` extra) |
| `ATELIER_DISTILL_DATABASE_URL` | (unset)                      | Distill Postgres URL (required when distill is enabled) |

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
- Read `STAGE_D_E_WALKTHROUGH.md` for the deep dive on fork-graph,
  snapshot-query, and the original 4-panel SPA (now at `/panels`).
- Read `STAGE_F_WALKTHROUGH.md` for the deep dive on per-turn freeze,
  the fork / snapshot-query dialogs, live-tail SSE, the HTTP turn
  fallback, and the CLI `session` subcommands.
- Read `STAGE_G_WALKTHROUGH.md` for the deep dive on the agent loop
  (`AgentRunner` + loom), live SSE broadcast, the memory layer
  (`MemoryService` + distill ingest / retrieve), and the chat shell
  at `/`.
- Remaining work is server-side: a real `DistillSnapshotResolver` to
  replace `EchoSnapshotResolver`, a `session tail` CLI to mirror the
  SPA's live tail, "spawn an agent on this version" buttons in the
  panels, and end-to-end auth wiring. See "Next steps" in
  `PROJECT_STATUS.md`.
