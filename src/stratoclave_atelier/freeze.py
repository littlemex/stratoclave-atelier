"""Freeze pipeline: events -> JSONL bytes -> BlobStore -> Version row.

Stage C exposes a single high-level entrypoint, :func:`freeze_session`,
that the freeze REST endpoint and tests both call. Keeping the pipeline
in one function (rather than scattering it across the router) means the
ordering constraints are explicit:

1. Resolve ``start_seq`` / ``end_seq`` against the live event log.
2. Reject empty ranges with :class:`ValueError` (the handler maps it to
   ``409 Conflict``).
3. Serialise the ``turn``-kind events to canonical JSONL bytes.
4. Write the bytes via :class:`BlobStore` (idempotent on identical
   content).
5. Insert a :class:`Version` row referencing the blob.

Only ``kind == "turn"`` events are serialised: control events
(``freeze`` / ``fork`` / ``system``) live in the event log for replay
but never end up in a frozen JSONL snapshot. This matches the freeze
semantics agreed in the design doc -- a Version is a recordable agent
conversation, not an audit trail.
"""

from __future__ import annotations

import json
from uuid import UUID

from stratoclave_atelier.blobs import BlobStore
from stratoclave_atelier.core import ConflictError
from stratoclave_atelier.core.types import Event, Version
from stratoclave_atelier.db import Store


def serialise_jsonl(events: list[Event]) -> bytes:
    """Encode turn events as canonical JSONL bytes.

    Each line is the raw ``payload`` re-emitted with sorted keys so that
    semantically identical content always hashes to the same SHA-256.
    Trailing newline is mandatory; the SHA covers it. ``ensure_ascii``
    is left at the default (True) so multi-byte characters render as
    ``\\uXXXX`` -- this avoids divergence between platforms with
    different default encodings.
    """

    if not events:
        return b""
    lines = [
        json.dumps(event.payload, sort_keys=True, separators=(",", ":"))
        for event in events
        if event.kind == "turn"
    ]
    return ("\n".join(lines) + "\n").encode("utf-8") if lines else b""


async def freeze_session(
    *,
    store: Store,
    blob_store: BlobStore,
    session_id: UUID,
    start_seq: int | None = None,
    end_seq: int | None = None,
    label: str | None = None,
) -> Version:
    """Freeze a session range into an immutable :class:`Version`.

    See module docstring for the pipeline. Raises
    :class:`stratoclave_atelier.core.errors.NotFoundError` if the
    session does not exist (propagated from
    :meth:`Store.list_events`), and :class:`ConflictError` if the
    requested range contains no turn events.
    """

    all_events = await store.list_events(session_id)
    turn_events = [e for e in all_events if e.kind == "turn"]
    if not turn_events:
        raise ConflictError(f"session {session_id} has no turn events to freeze")

    actual_start = start_seq if start_seq is not None else turn_events[0].seq
    actual_end = end_seq if end_seq is not None else turn_events[-1].seq
    if actual_end < actual_start:
        raise ConflictError(f"end_seq ({actual_end}) must be >= start_seq ({actual_start})")

    selected = [e for e in turn_events if actual_start <= e.seq <= actual_end]
    if not selected:
        raise ConflictError(
            f"no turn events in range [{actual_start}, {actual_end}] for session {session_id}"
        )

    payload = serialise_jsonl(selected)
    write = await blob_store.write(payload)
    return await store.create_version(
        session_id=session_id,
        blob_sha=write.sha256,
        blob_path=write.path,
        start_seq=selected[0].seq,
        end_seq=selected[-1].seq,
        byte_size=write.byte_size,
        label=label,
    )
