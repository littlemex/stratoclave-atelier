# Stratoclave Atelier -- Stage G Walkthrough

**Last updated**: 2026-05-27
**Scope**: Real agent loop via `stratoclave-loom`, cross-session memory
via `stratoclave-distill`, claude-capture-style chat at `/`, legacy
4-panel SPA moved to `/panels`.
**Status**: Done; merged once the `stage-g` PR lands on `main`.

This is the engineer-onboarding companion to `STAGE_F_WALKTHROUGH.md`.
Stages B-F shipped the storage spine (events / versions / blobs / fork
graph / snapshot queries) and a 4-panel operator UI that drove
ingest -> freeze -> fork -> snapshot end-to-end. Until now there was no
"agent" inside atelier: turns came from the WebSocket / HTTP ingest
endpoints and were authored by an external process. Stage G adds the
agent.

---

## 1. The 30-second mental model

```
                  Stage G additions
       +-----------------------------------------+
       | UI                                       |
       |   - claude-capture-style chat at /       |
       |   - 4-panel SPA still served at /panels  |
       |   - "memory: on" badge on user turn      |
       |                                          |
       | Backend                                  |
       |   - AgentRunner (loom claude_code)       |
       |   - SSE live broadcast (asyncio.Queue)   |
       |   - MemoryService (distill optional)     |
       |   - POST /api/sessions/{id}/agent-runs   |
       |                                          |
       | Memory                                   |
       |   - on freeze: distill IngestRunner      |
       |   - on run:    distill Retriever         |
       |   - <memory>...</memory> block prepended |
       +-----------------------------------------+
```

The chat at `/` is the new front door. A user types a prompt, atelier
creates a session if none exists, kicks off `AgentRunner.run()` against
the configured loom backend, and streams `text_delta` chunks back into
the same SSE channel that powers the panels. When freeze is invoked,
the selected turn range is handed to distill so future runs can pull
"canonical" / "emerging" facts from previous sessions and splice them
into the prompt.

---

## 2. New components, by file

### Agent loop (`stratoclave_atelier/agent_runner.py`)

`AgentRunner` owns one `stratoclave_loom.AgentSession` per atelier
session id. Sessions are kept warm in a process-local dict and disposed
on shutdown. Each `run()` call:

1. Optionally calls `MemoryService.retrieve(query=prompt)` (best-effort,
   exception-swallowing).
2. Persists a `kind="turn"` event with `role="user"`, the raw prompt,
   and `memory_used: bool`.
3. Streams `AcpChunk` items from the backend, persisting each
   `text_delta` chunk as a `kind="agent_chunk"` event for live tail.
4. Writes a final `kind="agent_turn"` event carrying the joined
   assistant text so freeze / replay sees the full response as one row.
5. On exception, persists `kind="agent_error"` and returns -- the
   exception never propagates through `schedule()`.

Backend selection is purely env-driven: `ATELIER_AGENT_BACKEND`
(`claude_code` / `kiro_code` / `none`). Tests override the resolved
`agent_backend` via `object.__setattr__` and register a stub backend
through `stratoclave_loom.register_backend`.

### Agent runs API (`api/agent_runs.py`)

`POST /api/sessions/{id}/agent-runs` returns `202 Accepted` and
schedules the run via `AgentRunner.schedule()`. The SPA listens on
`GET /api/sessions/{id}/events?follow=true` for the streamed response.
This keeps the chat surface and the panels surface looking at the
exact same event stream -- no second channel.

### SSE live broadcast (`events_bus.py`, `api/events.py`)

`EventBus` is a per-process pub-sub built on `asyncio.Queue`. The SSE
endpoint subscribes *before* it reads history, then dedupes replayed
events against the live queue via a `last_seen_seq` cursor. A `: ping`
keepalive every 15 s prevents reverse proxies from idle-closing the
connection. If the bus signals "you fell behind" (`None` sentinel) the
handler closes the response so the client reconnects with
`from_seq=last_seq` and resyncs through replay.

### Memory layer (`memory.py`, `_distill_memory.py`)

`MemoryService` is a `Protocol` with four async methods (`enabled`,
`ingest_session`, `retrieve`, `aclose`). Two implementations:

- `NoopMemoryService` -- always returns `None`; wired when
  `ATELIER_DISTILL_ENABLED=false` (the default) or when the optional
  `stratoclave-atelier[memory]` extra is missing.
- `DistillMemoryService` -- lives in `_distill_memory.py` so the
  distill import only happens when the extra is installed. Owns its own
  asyncpg pool, IngestRunner, and Retriever.

`build_memory_service(config)` is async. It checks `distill_enabled`,
then lazy-imports `_distill_memory.DistillMemoryService` -- on
ImportError it logs a warning and falls back to noop, so a server
boot never fails because of a missing optional dep.

`freeze_session()` calls `memory.ingest_session(events=selected)` after
the Version row is persisted. Failures inside the memory pipeline are
logged and never block the freeze.

`AgentRunner.run()` calls `memory.retrieve(query=prompt)` if
`memory_context` was not supplied by the caller. The result is
prepended as a `<memory>...</memory>` block before the user prompt and
the user turn event records `memory_used: True`.

### Frontend split (`frontend/static/`)

```
frontend/static/
+-- index.html              <- Stage G chat (default at /)
+-- css/chat.css
+-- js/chat.js
+-- panels/
    +-- index.html          <- Stage B-F 4-panel SPA at /panels
    +-- css/app.css
    +-- js/app.js
```

`server.py::_mount_frontend` wires `/` to `chat`, `/panels` to the
legacy SPA, and shares `/static/*` for both asset trees.

The chat SPA does three things:

- creates a session lazily on first prompt,
- subscribes to the SSE stream and renders `turn` (user, with optional
  "memory: on" badge), `agent_chunk` (text_delta -> streaming bubble),
  and `agent_turn` (final assistant text) events,
- exposes a "Freeze" button that calls `POST /api/sessions/{id}/freeze`
  with an empty body (whole session).

Power-user actions (groups, fork dialog, snapshot-query dialog,
multi-session view) live at `/panels` unchanged.

---

## 3. Wire-level walkthrough

```
1. Browser loads /
2. User types "summarise X" + Enter
3. SPA: POST /api/sessions   {title:"chat session"} -> 201
4. SPA: POST /api/sessions/{id}/agent-runs {prompt:"..."} -> 202
5. AgentRunner.schedule() spawns asyncio.Task
6. Task -> memory.retrieve(query="summarise X")
7. Task persists turn event (role=user, memory_used=true)
8. EventBus broadcasts -> SSE subscribers get "turn" event
9. Task streams chunks from loom -> agent_chunk events
10. SPA renders streaming bubble character-by-character
11. Task persists agent_turn (final assistant text)
12. User clicks "Freeze"
13. SPA: POST /api/sessions/{id}/freeze {} -> 201
14. freeze_session writes JSONL blob + Version row
15. memory.ingest_session(events=selected)  (best-effort)
```

---

## 4. Configuration

| Env var | Default | Meaning |
|---------|---------|---------|
| `ATELIER_AGENT_BACKEND` | `none` | `claude_code`, `kiro_code`, or `none` |
| `ATELIER_AGENT_CWD` | unset | working directory for the loom session (required when backend != `none`) |
| `ATELIER_AGENT_ALLOWED_TOOLS` | unset | comma-separated allowlist passed to loom |
| `ATELIER_AGENT_MEMORY` | `true` | per-server toggle for memory retrieval |
| `ATELIER_DISTILL_ENABLED` | `false` | whether to wire `DistillMemoryService` |
| `ATELIER_DISTILL_DATABASE_URL` | unset | distill Postgres URL (required when distill enabled) |

Memory is opt-in twice over: `ATELIER_DISTILL_ENABLED=true`
*and* `ATELIER_AGENT_MEMORY=true`. Either knob set to `false` demotes
to no cross-session context.

---

## 5. Why it is shaped this way

### Why the chat lives at `/` and panels at `/panels`

The 4-panel UI is invaluable for debugging the fork DAG and inspecting
events, but it is not the right surface for a casual user wanting an
agent. claude-capture's single-textarea front door is the canonical
"chat" UX, and copying it costs nothing because the SSE stream is
already there.

### Why `MemoryService` is a Protocol with a Noop default

Distill brings a Postgres pool, an embedding provider, and an LLM
provider. We did not want any of that to be load-bearing for atelier
boot or for the unit-test path. A `Protocol` plus a Noop default lets
the rest of the code call `memory.retrieve()` unconditionally and lets
tests inject recording stubs without touching distill at all.

### Why `freeze_session` swallows memory exceptions

A frozen `Version` row on disk is the source of truth. Memory ingest
is enrichment. If the LLM provider hiccups during distillation we do
not want the freeze to roll back -- the user's bytes are already safe.
The exception is logged so operators can re-ingest later via the
distill CLI.

---

## 6. Test surface

| Layer | What | Where |
|-------|------|-------|
| Unit  | `AgentRunner` happy path, error path, memory injection | `tests/unit/test_agent_runner.py`, `test_memory.py` |
| Unit  | `POST /api/sessions/{id}/agent-runs` 202 / 503 / 404 / 409 / 422 | `tests/unit/test_api_agent_runs.py` |
| Unit  | SSE replay + live tail + keepalive + dedupe | `tests/unit/test_api_events.py` |
| Unit  | `MemoryService` Protocol + Noop + `build_memory_service` paths | `tests/unit/test_memory.py` |
| Unit  | `freeze_session` calls `memory.ingest_session` | `tests/unit/test_memory.py::test_freeze_invokes_memory_ingest` |
| Unit  | Stage G chat at `/` + panels at `/panels` + asset routing | `tests/unit/test_frontend_mount.py` |

---

## 7. Pointers

- `stratoclave-loom`: `~/stratoclave-loom/` -- backend abstraction.
- `stratoclave-distill`: `~/stratoclave-distill/` -- session
  distillation and learning aggregation.
- Stage F walkthrough: `docs/STAGE_F_WALKTHROUGH.md`.
- Stage D + E walkthrough: `docs/STAGE_D_E_WALKTHROUGH.md`.
