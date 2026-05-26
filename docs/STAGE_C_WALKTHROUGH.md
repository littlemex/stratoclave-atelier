# Stratoclave Atelier — Stage C Walkthrough

**Last updated**: 2026-05-26
**Scope**: BlobStore + WebSocket ingest + freeze (whole / range) + SSE replay.
**Status**: Done; merged once the `stage-c` PR lands on `main`.

This document is the engineer-onboarding companion to `STAGE_A_B_WALKTHROUGH.md`.
It explains the four moving parts that Stage C added, why they were
shaped that way, and where the corner cases live.

---

## 1. The 30-second mental model

```
                   ingest                       freeze
        ┌────────────────────────┐    ┌──────────────────────────┐
WS  ───►│ append_event(turn=N)   │    │  list_events             │
        │  -> events table        │    │  -> JSONL bytes (turns only)│
        │  -> ack {seq: N}       │    │  -> BlobStore.write       │
        └────────────────────────┘    │  -> create_version        │
                                      └──────────────────────────┘
                  SSE
        GET /events?from_seq=N
        -> stream of historical events
```

* **Events are the source of truth.** Every turn that an agent emits
  goes through `append_event` -- nowhere else. Ingest writes them in,
  SSE reads them out, freeze re-serialises a slice into JSONL.
* **Versions are derived.** A `Version` row is just a pointer to a
  content-addressed blob plus a `[start_seq, end_seq]` range. Freezing
  the same content twice points two `Version` rows at one blob.
* **Blobs are immutable.** Once a blob lands at its final
  `<root>/sha256/<aa>/<full>.jsonl` path it is `chmod 0444`. Two writers
  racing on the same digest is fine because both produced identical bytes.

---

## 2. BlobStore (`stratoclave_atelier.blobs`)

**File**: `src/stratoclave_atelier/blobs/store.py`

### Interface

```python
class BlobStore(Protocol):
    async def write(self, payload: bytes) -> WriteResult: ...
    async def read(self, sha256: str) -> bytes: ...
    async def exists(self, sha256: str) -> bool: ...

@dataclass(frozen=True, slots=True)
class WriteResult:
    sha256: str       # 64-char hex digest of payload
    path: str         # absolute path on disk (or mem://... in tests)
    byte_size: int    # len(payload)
    existed: bool     # True iff the blob was already on disk
```

### Two implementations

* `FileBlobStore(root)` -- production. Stores under
  `<root>/sha256/<aa>/<full>.jsonl`. The first two hex characters are
  the directory fan-out (the same trick git uses for loose objects).
* `InMemoryBlobStore()` -- tests. Dict-backed, mimics the same
  semantics including `existed=True` on duplicate writes.

### Why "write-once + 0444"

Atelier promises that frozen versions are immutable. If the disk
supported overwriting, a bug elsewhere could mutate a version's bytes
and the SHA on disk would no longer match the SHA in
`versions.blob_sha`. Writing through a temp file + rename + chmod 0444
makes that mutation explicit (root has to `chmod u+w` first), which is
what we want -- accidental mutation should fail loudly.

### Atomic-write recipe

```
1. final = root/sha256/<aa>/<full>.jsonl
2. if final exists -> return existed=True (idempotent)
3. tmp = final + ".tmp.<pid>.<random>"
4. open(tmp, "xb"), write, fsync
5. os.replace(tmp, final)            # POSIX atomic
6. chmod(final, 0o444)
```

`fsync` is not strictly required for correctness on a fast SSD, but it
guarantees that a crash between rename and the next event commit won't
leave a 0-byte version row pointing at a non-existent blob.

### Race semantics

If two writers both race for the same digest, exactly one wins
`os.replace`. The loser sees `FileExistsError` on the open (because
another writer landed first) or the final exists check on retry. Both
outcomes return `existed=True` -- the bytes are identical by
content-addressing, so callers never observe corruption.

---

## 3. Freeze pipeline (`stratoclave_atelier.freeze`)

**File**: `src/stratoclave_atelier/freeze.py`

### Public API

```python
async def freeze_session(
    *,
    store: Store,
    blob_store: BlobStore,
    session_id: UUID,
    start_seq: int | None = None,
    end_seq: int | None = None,
    label: str | None = None,
) -> Version:
```

### What happens inside

1. `list_events(session_id)` -- fetch the full log.
2. Filter to `kind == "turn"`. Control events (`freeze` / `fork` /
   `system`) live in the log for replay but never end up in a frozen
   JSONL. *A Version is the recordable conversation, not the audit
   trail.*
3. Resolve `start_seq` / `end_seq` (default: full turn range).
4. Reject empty / inverted ranges with `ConflictError`.
5. Re-serialise to JSONL bytes. Each line is
   `json.dumps(payload, sort_keys=True, separators=(",", ":"))` so the
   SHA-256 is stable across machines and Python releases.
6. `blob_store.write(bytes)` -- get a `WriteResult` (idempotent on
   identical content).
7. `store.create_version(...)` -- insert the row that points at the blob.

### Why `sort_keys=True, separators=(",", ":")`

Two semantically identical turns must hash to the same SHA-256 even if
the agent emitted keys in a different order or with different
whitespace. Without canonicalisation, freezing the same conversation
twice could produce two distinct blobs and break the
content-addressing invariant.

---

## 4. WebSocket ingest (`stratoclave_atelier.api.ingest`)

**File**: `src/stratoclave_atelier/api/ingest.py`

### Wire format

* Endpoint: `WS /api/sessions/{session_id}/ingest`
* Each message is **one JSON object** per WebSocket text frame.
* Server response per accepted message:
  ```json
  {"type": "ack", "seq": 0, "event_id": "..."}
  ```
* Server response on validation failure:
  ```json
  {"type": "error", "code": "invalid_json", "detail": "..."}
  ```

### Close codes

| Code | Meaning |
|------|---------|
| 1003 | Frame was non-text or non-JSON (RFC 6455 "unsupported data") |
| 1011 | Internal error from the store |
| 4404 | Application-defined: target session does not exist |
| 4423 | Application-defined: session is not active (frozen / archived) |

### Why no spool file on disk

Stage C originally allowed for a "spool JSONL on the server side" path
so freeze could `cat spool` instead of replaying events. We dropped it
because:

1. The events table already has every turn with a stable `seq`. There
   is no extra information in a spool file.
2. Two stores (events + spool) need to stay in sync. That sync is
   exactly the kind of bug we are trying to avoid by making
   `events.payload` the single source of truth.
3. Per-turn freeze already needs to walk events anyway -- adding spool
   handling on top would be pure cost.

If future profiling shows freeze-from-events is too slow on long
sessions, we can introduce a derived "rolled-up snapshot" cache without
changing the wire protocol. For now, simpler beats faster.

---

## 5. SSE replay (`stratoclave_atelier.api.events`)

**File**: `src/stratoclave_atelier/api/events.py`

### Endpoint

`GET /api/sessions/{session_id}/events?from_seq=N`

Returns `text/event-stream` with one frame per event:

```
id: <seq>
event: <kind>
data: {"event_id": "...", "session_id": "...", "seq": N, "kind": "...", "payload": {...}, "created_at": "..."}

```

### What this is for

UI reconnect. When the operator reloads the atelier page, the client
sends `from_seq = lastSeenSeq + 1` and the server backfills everything
since. The handler closes the stream when the historical replay is
done -- it does not currently keep the socket open for live tail.

### What this isn't (yet)

* **No live tail.** Adding it later via Postgres `LISTEN/NOTIFY` or a
  redis-backed bus is purely additive: clients can detect end-of-stream
  via the empty SSE body and reconnect with the next `from_seq`.
* **No Last-Event-ID header.** We respect `from_seq` query-param only.
  The HTML5 SSE spec auto-reconnect uses the `Last-Event-ID` header,
  which we'll wire when we ship the UI in Stage E.

---

## 6. Where each stage's tests live

```
tests/unit/
├── test_blob_store.py        Stage C-1
├── test_freeze.py            Stage C-3 (pipeline level)
├── test_api_freeze.py        Stage C-3 (REST level)
├── test_api_ingest.py        Stage C-2
└── test_api_events.py        Stage C-4

tests/integration/
└── test_asyncpg_store.py     adds end-to-end freeze test against real Postgres
```

Run them locally with:

```bash
.venv/bin/python -m pytest tests/unit -q
ATELIER_TEST_DATABASE_URL="postgresql+asyncpg://atelier:atelier@localhost:5432/atelier" \
  .venv/bin/python -m pytest tests/integration -q -m integration
```

CI runs the same two phases as separate jobs (`test (3.11)`,
`test (3.12)` for unit; `integration` for asyncpg).

---

## 7. What's intentionally not in Stage C

* **Live SSE tail** -- see Section 5.
* **Auth on the WebSocket** -- ingest currently relies on
  application-level network controls. Adding bearer-token auth lands
  with the rest of the auth wiring in Stage D / E.
* **Blob garbage collection** -- versions never get deleted, so blobs
  never need to be reaped. If we add archive-and-purge later, GC reads
  `versions.blob_sha` and removes any blob whose SHA isn't referenced.
* **Compression** -- frozen JSONL stays uncompressed. The event log
  rarely exceeds 100 KB / session and the operator-facing tooling
  benefits from `cat`-able blobs. We can wrap with zstd later behind a
  transparent decode in `BlobStore.read` if size becomes a concern.

---

## 8. One-line summary

> Stage C made conversations recordable: a websocket pipes turns into
> the event log, freeze re-serialises any turn range into a
> content-addressed blob with an immutable `Version` pointer, and SSE
> lets the UI rehydrate state on reconnect.
