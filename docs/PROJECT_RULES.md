# stratoclave-atelier: Project Rules

**Last updated**: 2026-05-27

These are the rules that apply to this repository specifically. They
exist on top of the more general 4-OSS series conventions and the
no-hardcode policy that all stratoclave repos share.

## Coding conventions

### Languages and runtime

- **Python 3.11+** is required. Use modern syntax: `X | None` over
  `Optional[X]`, `dict[str, int]` over `Dict[str, int]`, `Self` from
  `typing`, structural pattern matching where it clarifies intent.
- All public dataclasses are `frozen=True, slots=True` unless there is
  a specific reason not to be.
- Type-check with `mypy --strict`. We treat type errors as build
  failures.

### File layout

```
src/stratoclave_atelier/
  __init__.py              -- public surface (small)
  config.py                -- AtelierConfig (env-driven)
  core/
    __init__.py
    errors.py              -- AtelierError + subclasses
    types.py               -- Group / Session / Version
  db/                      -- (Stage B) Store Protocol + impls
  api/                     -- FastAPI routers
    __init__.py
    health.py
  server.py                -- create_app() + module-level `app`
  cli.py                   -- argparse CLI: serve / migrate / config

migrations/                -- alembic scripts (SQL-only)
tests/
  unit/                    -- no Docker, no network, no provider keys
  integration/             -- gated by `integration` marker + ATELIER_TEST_DATABASE_URL
docs/                      -- 3 mandatory docs (English)
.github/workflows/         -- CI definitions
```

### No hard-coded values (project-wide rule)

This repo enforces the same no-hardcode policy as the rest of the
stratoclave series:

- Database URLs, ports, hostnames, blob directories, auth tokens,
  feature flags -- **all via `AtelierConfig` / env vars**.
- The CI smoke check (`scripts/check-no-hardcoded-secrets.sh`) is a
  hard gate; PRs with new hard-coded values will be rejected.
- The only place the literal value of a default may appear in `src/`
  is inside `config.py` (as a `_DEFAULT_X` module-level constant).

If you find yourself wanting to put a URL or a magic number into a
non-config module, route it through `AtelierConfig` instead.

### Async style

- All endpoint handlers are `async def`.
- Use `asyncpg` for DB access; do not mix in synchronous psycopg
  calls except inside the alembic migration env.
- Cancellation safety matters: long-running endpoints (SSE / WebSocket)
  must drop their resources cleanly when the client disconnects.

### Error handling

- Raise subclasses of `AtelierError` (`ConfigError`, `SchemaError`,
  `NotFoundError`) from the domain layer.
- Translate them into HTTP error responses in the API layer
  (FastAPI exception handlers), not at the call site.

## Testing policy

- **Unit tests** must run without Docker, network, or external state.
  Use the `InMemoryStore` (Stage B onwards) instead of asyncpg.
- **Integration tests** require `ATELIER_TEST_DATABASE_URL` and run the
  full alembic migration before each session.
- Mark slow tests with `@pytest.mark.slow` and exclude them from the
  fast test path (`pytest -m "not slow"`).
- Aim for 80%+ branch coverage on `src/stratoclave_atelier/core/` and
  `src/stratoclave_atelier/db/`.

## Documentation policy

- **All project markdown is in English.** This includes README, the 3
  mandatory docs, CONTRIBUTING, SECURITY, CODE_OF_CONDUCT, design docs,
  and changelog. Conversation in PRs and issues may be in Japanese.
- The 3 mandatory docs (`docs/GETTING_STARTED.md`,
  `docs/PROJECT_STATUS.md`, `docs/PROJECT_RULES.md`) must be kept
  current. Update them in the same PR that ships the corresponding
  code change.
- Avoid mermaid diagrams unless the diagram is genuinely the clearest
  way to express the idea. Prose plus a short list usually wins.
- Never introduce a new top-level markdown file in the repository
  root; new docs go under `docs/`.

## Git workflow

- Branch from `main`: `feat/<short>`, `fix/<short>`, `docs/<short>`,
  `chore/<short>`.
- Commit messages follow Conventional Commits
  (`feat(api): ...`, `fix(versions): ...`).
- **No `Co-Authored-By` lines** in commit messages.
- **No emojis** anywhere in source, commit messages, or docs.
- Local `git push` is blocked by Code Defender; pushes go through the
  S3+server procedure documented in the operator runbook.

## Auth modes

| Mode                    | Use case                                                        |
|-------------------------|-----------------------------------------------------------------|
| `none`                  | Local development; trust localhost only                         |
| `bearer`                | Single shared token; suitable for a small team / pinned UI      |
| `stratoclave_cognito`   | Delegate to stratoclave's Cognito user pool (planned, Stage D+) |

`auth_mode=none` should never be deployed to a publicly reachable host.
The `AtelierConfig` constructor refuses to accept `auth_mode=bearer`
without a non-empty `bearer_token`.

## Scope boundaries (do not cross)

These are permanent out-of-scope items. Do not add code that
introduces them, even speculatively:

- Memo features.
- Remote machine connection / SSH orchestration.
- claude-capture data migration.
- claude-capture API compatibility.

If a use case appears that genuinely needs one of these, escalate to
a maintainer and write a design doc first.

## Performance targets

- Cold start (uvicorn import + `/healthz` 200): under 1s on a laptop.
- Fork DAG JSON for a 100-node graph: under 50ms server-side.
- JSONL freeze of a 10MB session: under 500ms (excluding fsync).

These are aspirational for Stage A but become hard targets in CI from
Stage C onwards.
