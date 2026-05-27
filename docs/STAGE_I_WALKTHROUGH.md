# Stage I Walkthrough: Distill snapshot resolver + CLI session tail

**Last updated**: 2026-05-27

Stage I closes two of the highest-priority Stage G/H follow-ups:

1. **`DistillSnapshotResolver`** -- the cross-session snapshot RPC
   (`POST /api/sessions/{id}/snapshot-query`) gains a real, distill-backed
   resolver. The Stage D `EchoSnapshotResolver` stays in place as the
   default for tests and the walking skeleton.
2. **`session tail` CLI subcommand** -- operators can now subscribe to
   a session's SSE event stream from the terminal and pipe each event as
   one JSON line into `jq`, downstream tooling, or just a pager.

Together, the two changes round out the Stage F + G surfaces: Stage F
gave the SPA a live tail, Stage G gave snapshot-query a `Retriever`,
and Stage I exposes both through the CLI / REST seam without coupling
the snapshot resolver to a specific LLM provider.

## What changed

### Source layout

| Area | Files |
|------|-------|
| Resolver | `src/stratoclave_atelier/snapshot_resolver.py` (new `DistillSnapshotResolver`, helpers `_summarize_turn_roles` and `_format_retrieval`) |
| Wiring | `src/stratoclave_atelier/server.py::lifespan` (build `DistillSnapshotResolver` when `cfg.snapshot_resolver == "distill"`, close pool on shutdown) |
| CLI | `src/stratoclave_atelier/cli.py::_cmd_session_tail` (httpx streaming + SSE frame parser) and the `session tail` subparser |
| Tests | `tests/unit/test_snapshot_resolver.py` (5 new test cases), `tests/unit/test_cli.py` (3 new test cases) |

No migrations and no new Python deps were introduced; both features
rely on the Stage G `[memory]` extra (already provides
`stratoclave-distill` + `asyncpg`) and on the Stage F `httpx` runtime
dependency.

### Config knob

`ATELIER_SNAPSHOT_RESOLVER` (default `echo`):

* `echo` -- keeps the deterministic resolver. No additional config.
* `distill` -- builds `DistillSnapshotResolver`. Requires
  `ATELIER_DISTILL_ENABLED=true` and `ATELIER_DISTILL_DATABASE_URL`,
  and the `[memory]` extra must be installed
  (`pip install -e '.[memory]'`).

The `distill` mode is cross-validated at config build time so a
misconfiguration (e.g. `ATELIER_SNAPSHOT_RESOLVER=distill` without
`ATELIER_DISTILL_ENABLED=true`) fails fast with a `ConfigError` rather
than silently falling back to echo.

## DistillSnapshotResolver

### Output shape

`DistillSnapshotResolver.resolve()` returns a multi-line string with
four sections, in this order:

```text
[distill] version=<label-or-unlabeled> turns=N start_seq=A end_seq=B
query: <the user's question, verbatim>
turn-roles: <role>=<count>, <role>=<count>, ...    # role histogram
memory:                                            # only when retrieval has hits
[canonical] <rule>
[emerging] <rule>
[gap] <topic>
```

When there are no retrieval hits (or distill is reachable but the query
embeds to nothing useful), the last section collapses to `memory: <none>`
so the audit row still records "we asked the resolver and it found
nothing" rather than an ambiguous truncation.

### Why no LLM call?

The Stage I scope deliberately stops short of "summarise the JSONL
through an LLM". Three reasons:

1. **Determinism for tests.** The Stage D Echo resolver was already
   deterministic; Stage I keeps the snapshot answer reproducible so
   `tests/unit/test_snapshot_resolver.py` does not need a mocked LLM
   provider.
2. **Latency budget.** The handler is synchronous from the client's
   perspective: an LLM round-trip would require streaming or background
   processing, neither of which is wired up yet.
3. **Composability.** A future Stage J can extend
   `DistillSnapshotResolver` (or wrap it) to add an LLM call -- the
   blob bytes and retrieval hits are already loaded, so the integration
   is "plug an LLM provider and a prompt" rather than re-architecting.

### Lifespan ownership

The resolver opens its own asyncpg pool via
`stratoclave_distill.db.asyncpg.open_pool`. The lifespan callback
records ownership in a local `owns_resolver` flag so:

* an externally-injected resolver (passed to `create_app(snapshot_resolver=...)`)
  is **not** closed -- the caller stays responsible.
* a resolver built from config **is** closed on shutdown via `aclose()`,
  in addition to the existing `memory.aclose()` and `runtime_store.dispose()`.

The pool is independent of the Stage G `MemoryService` pool so the two
subsystems stay decoupled. If pool count becomes a problem in
production, a future stage can hoist a single shared pool into the
config and pass it to both services.

### Failure modes

| Scenario | Behaviour |
|----------|-----------|
| `BlobStore.read(blob_sha)` raises `FileNotFoundError` | `turn-roles` falls back to `<empty>`. The header + query + memory sections are still emitted. |
| `retriever.retrieve(query)` raises | Logged via `logger.exception`; `memory: <none>` is rendered. The handler does not 5xx. |
| Empty query string | Retrieval is skipped entirely (defensive: avoids embedding whitespace). Output is still well-formed. |
| `aclose()` called twice | Idempotent; the pool is closed at most once. |

## CLI `session tail`

### Surface

```text
stratoclave-atelier session [--base-url URL] tail SESSION_ID
                                              [--from-seq N] [--no-follow]
```

Wire shape: the subcommand opens an `httpx.Client` with `timeout=10s` for
the connect phase and `read=None` for the body, then issues
`GET /api/sessions/{id}/events?from_seq=N&follow=true|false` via
`client.stream(...)` so memory does not balloon on long replays.

For each line in the response:

* `: ping` keepalives are dropped silently.
* Lines starting with `id:` / `event:` are dropped (they're the SSE
  framing siblings of `data:`).
* Lines starting with `data:` are parsed as JSON, re-serialized with
  `sort_keys=True`, and emitted as one stdout line. Non-JSON `data:`
  payloads are surfaced raw rather than dropped, so unexpected frames
  remain visible.

A 4xx/5xx response status produces `error: GET ... -> {status}: {body}`
on stderr and exits 2, matching the existing `_request` helper in the
CLI. Ctrl-C exits 0 cleanly.

### Example session

```bash
# Tail a session live (default: from seq 0, follow=true).
stratoclave-atelier session tail 7cf3eec0-c5c9-407c-8a36-ad7275bebaf8 \
  | jq -c '{seq, kind, payload}'

# Drain history only and exit.
stratoclave-atelier session tail 7cf3eec0-c5c9-407c-8a36-ad7275bebaf8 \
  --from-seq 100 --no-follow > replay.jsonl
```

The output is structurally identical to what the SPA's `EventSource`
sees, so the CLI can be used as a drop-in debug tool when the panels
UI is not handy.

### Tests

Three new tests in `tests/unit/test_cli.py` cover the wire-level
contract by stubbing `httpx.Client` with a streaming response:

* **happy path** -- two SSE events produce two JSON-line stdout rows;
  `: ping` keepalives are dropped.
* **--no-follow** -- the request is issued with `follow=false` and the
  user-supplied `--from-seq` is forwarded.
* **4xx** -- the body is surfaced on stderr and the exit code is 2.

## Verification

```bash
ruff check src tests
ruff format --check src tests
mypy
pytest -x -q tests/unit
```

All four pass after the Stage I changes:

* 173 unit tests (was 162 in Stage H; +5 resolver tests, +3 CLI tail
  tests, plus minor fixtures).
* mypy strict over the 32 source files (tests are excluded by config).
* ruff clean.

## Operating the new resolver

To run the chat shell with a real distill-backed snapshot resolver:

```bash
export ATELIER_DATABASE_URL="postgresql+asyncpg://atelier:atelier@localhost:5432/atelier"
export ATELIER_DISTILL_ENABLED=true
export ATELIER_DISTILL_DATABASE_URL="postgresql+asyncpg://distill:distill@localhost:5432/distill"
export ATELIER_SNAPSHOT_RESOLVER=distill

# Optional distill provider knobs (passed straight to DistillerConfig.from_env):
export DISTILL_LLM_PROVIDER=anthropic
export DISTILL_LLM_API_KEY=sk-...
export DISTILL_EMBEDDING_PROVIDER=openai
export DISTILL_EMBEDDING_API_KEY=sk-...

stratoclave-atelier serve --port 8123
```

Issue a snapshot query against any frozen Version (panels UI ->
"Snapshot query" dialog, or `stratoclave-atelier session snapshot-query
... --query "..."`); the audit row in `snapshot_queries.response`
will now contain the four-section distill answer instead of the
`[echo] ...` stub.

To quickly tail an active session in another terminal:

```bash
ATELIER_BASE_URL=http://localhost:8123 \
  stratoclave-atelier session tail <session-id> | jq -c .
```

## What's still on the punch list

`PROJECT_STATUS.md::Next steps` is now down to:

1. Loom "spawn on version" button.
2. Auth wiring (bearer / Cognito) through the SPA.
3. Memory ingestion observability in the panels UI.
4. Optional LLM-backed snapshot resolver (extension on top of Stage I).

Stage I lays the groundwork for #4 -- the JSONL bytes and retrieval
hits are both already loaded inside `resolve()`, so the next step is
to call an LLM provider with both as context.
