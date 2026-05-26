# Stratoclave Atelier -- Stage H Walkthrough

**Last updated**: 2026-05-27
**Scope**: Per-session backend selection (claude_code / kiro_code /
mock) via the chat UI, persisted on `sessions.agent_backend`, validated
against the operator-allowed list.
**Status**: Done; rolled up with the Stage G chat surface.

Stage G hard-wired the loom backend at the server level: every session
on a given deployment talked to whichever backend
`ATELIER_AGENT_BACKEND` named. That works in single-tenant setups but
becomes painful as soon as one operator wants to run claude_code,
kiro_code, and mock side-by-side from the same chat surface. Stage H
fixes that without changing the storage spine: it adds a
`<select>` to the chat header, persists the choice on the `Session`
row, and threads the per-session backend through the `AgentRunner`.

---

## 1. The 30-second mental model

```
                  Stage H additions
       +------------------------------------------+
       | UI                                       |
       |   - Backend <select> in chat header      |
       |   - Locks once a session is warm         |
       |   - Unlocks on "New session"             |
       |                                          |
       | API                                      |
       |   - GET  /api/agent/backends             |
       |   - POST /api/sessions  (agent_backend)  |
       |   - POST /api/sessions/{id}/fork         |
       |     (inherits parent.agent_backend)      |
       |                                          |
       | Storage                                  |
       |   - sessions.agent_backend TEXT NULL     |
       |     CHECK ('claude_code'/'kiro_code'     |
       |             /'mock')                     |
       |                                          |
       | Runner                                   |
       |   - AgentRunner._resolve_backend_for     |
       |   - per-session warm cache               |
       |   - per-backend cwd / allowed_tools      |
       +------------------------------------------+
```

A user picks "kiro_code" from the dropdown, types a prompt, and atelier
creates a session whose `agent_backend = 'kiro_code'`. The
`AgentRunner` looks up `cwd_for_backend('kiro_code')` and warms a loom
`AgentSession` against the kiro-cli binary. Forks inherit the parent's
backend by default so a mid-conversation fork stays on the same engine.

---

## 2. Configuration model

Three layers, evaluated highest-priority-first:

1. **Per-session** -- `Session.agent_backend` (set at create / fork
   time, persisted in Postgres).
2. **Server default** -- `ATELIER_AGENT_BACKEND` (Stage G knob, kept
   for back-compat).
3. **Allowed list** -- `ATELIER_AGENT_BACKENDS_ALLOWED` (CSV: which
   backends the chat picker may offer).

```bash
# Multi-backend deployment
export ATELIER_AGENT_BACKENDS_ALLOWED="claude_code,kiro_code,mock"
export ATELIER_AGENT_BACKEND="claude_code"  # default if picker untouched
export ATELIER_AGENT_CWD="/srv/agents/wk"   # shared default

# Per-backend overrides (optional)
export ATELIER_AGENT_CWD_KIRO_CODE="/srv/agents/kiro-wk"
export ATELIER_AGENT_ALLOWED_TOOLS_CLAUDE_CODE="shell.run,file.read"
export ATELIER_AGENT_ALLOWED_TOOLS_KIRO_CODE="file.read"
```

Validation rules (all enforced in `AtelierConfig.__post_init__`):

* every entry of `agent_backends_allowed` must be a known backend
  (`claude_code` / `kiro_code` / `mock`);
* every allowed backend must have *some* cwd configured (per-backend
  override or the global default);
* `agent_backend`, when not `'none'`, must appear in
  `agent_backends_allowed` -- you can't have a default the picker
  doesn't list.

Stage G back-compat: if `agent_backends_allowed` is empty but
`agent_backend != 'none'`, the singular default becomes the implicit
allowed list.

---

## 3. Wire-level changes

### 3.1 New endpoint

```http
GET /api/agent/backends
200 OK
{
  "backends": [
    {"name": "claude_code", "ready": true, "cwd": "/srv/agents/wk"},
    {"name": "kiro_code",   "ready": true, "cwd": "/srv/agents/kiro-wk"},
    {"name": "mock",        "ready": true, "cwd": "/srv/agents/wk"}
  ],
  "default": "claude_code"
}
```

`ready` is `False` when the backend has no cwd configured (the picker
greys it out instead of failing hard at run-time).

### 3.2 `POST /api/sessions` accepts `agent_backend`

```http
POST /api/sessions
{"title": "kiro experiment", "agent_backend": "kiro_code"}

201 Created
{
  "session_id": "...",
  "agent_backend": "kiro_code",
  ...
}
```

* Unknown backend -> 409 (`backend 'wizard' is not in the allowed list`).
* No backends configured at all -> 409 (`set ATELIER_AGENT_BACKENDS_ALLOWED`).
* Omitting the field is fine; `agent_backend` becomes `null` and the
  runner falls back to the server default at run-time.

### 3.3 Forks inherit by default

```http
POST /api/sessions/{parent_id}/fork
{"title": "child", "parent_version_id": "...", "fork_seq": 5}
```

The child inherits `parent.agent_backend` unless the request body
overrides it. Mid-conversation forks therefore stay on the same engine
(otherwise context that the agent built up against the original backend
would be useless to the new one).

---

## 4. Storage migration

`migrations/versions/0002_session_agent_backend.py`:

```sql
ALTER TABLE sessions
  ADD COLUMN agent_backend TEXT
  CHECK (agent_backend IN ('claude_code','kiro_code','mock'));

CREATE INDEX idx_sessions_agent_backend ON sessions(agent_backend);
```

The column is `NULL`-able so existing Stage G rows keep their meaning
("use whatever the server default was when the run happened").

---

## 5. UI behaviour

`frontend/static/index.html` adds:

```html
<label class="chat-backend-label">
  <span>Backend:</span>
  <select id="chat-backend" data-testid="chat-backend"></select>
</label>
```

`frontend/static/js/chat.js`:

* On boot, `loadBackends()` calls `/api/agent/backends`, populates the
  `<select>`, and pre-selects the server default (or the first ready
  entry).
* `ensureSession()` includes `agent_backend` in the `POST /api/sessions`
  body when the picker has a value.
* Once a session exists, `lockBackendPicker(true)` disables the
  `<select>` until the user clicks "New session" -- the choice is
  intentionally sticky so all turns of one chat stay on one engine.
* The session label in the chat actions row shows
  `session: 1234abcd · kiro_code` so operators can see the engine at
  a glance.

---

## 6. Local verification (kiro-cli already installed)

```bash
# 1. Start a Postgres + atelier dev stack
export ATELIER_DATABASE_URL="postgresql+asyncpg://atelier:atelier@localhost:5432/atelier"
export ATELIER_AGENT_BACKENDS_ALLOWED="claude_code,kiro_code,mock"
export ATELIER_AGENT_BACKEND="kiro_code"
export ATELIER_AGENT_CWD="$PWD/.atelier-wk"
mkdir -p "$ATELIER_AGENT_CWD"

uv run stratoclave-atelier serve

# 2. Open http://localhost:8000/
# 3. The Backend picker lists claude_code / kiro_code / mock.
#    Pick "kiro_code", type "hello", press Send.
# 4. Network tab: POST /api/sessions includes "agent_backend":"kiro_code".
#    POST /api/sessions/{id}/agent-runs returns 202.
#    SSE stream shows text_delta chunks from kiro-cli.
# 5. Click "New session" -> the picker re-enables.
```

---

## 7. Test inventory

```
tests/unit/test_config.py        -- 11 new cases for Stage H config
tests/unit/test_api_agent.py     -- new file, 3 cases for /api/agent/backends
tests/unit/test_api_sessions.py  -- 6 new cases (create+fork w/ backend)
tests/unit/test_agent_runner.py  -- 5 new cases (resolve_backend_for / per-session warm)
```

Run:

```bash
.venv/bin/python -m pytest tests/unit/ -q
.venv/bin/ruff check src/ tests/
.venv/bin/ruff format --check src/ tests/
.venv/bin/python -m mypy src/stratoclave_atelier/
```

All green at HEAD of the stage-h branch (165 unit tests).

---

## 8. What we deliberately did not do

* **No migration data backfill**. Existing `agent_backend = NULL` rows
  fall through to the server default at run-time; no surprise rewrites.
* **No multi-backend-per-session**. One session, one engine. If you
  want a different backend, fork or start a new session.
* **No runtime backend swap**. The picker is locked once a session is
  warm: switching engines mid-stream would corrupt the loom session's
  internal state.
* **No tenant-scoped allow list**. Allowed backends are
  deployment-wide; multi-tenant scoping lives outside the atelier
  surface.
