"""Cross-session snapshot query resolver.

Stage D introduces a single new RPC: "given a frozen :class:`Version`,
answer this question". The actual resolver is pluggable: in production
it would dispatch to an LLM with the version's JSONL bytes loaded as
context; for the walking skeleton + unit tests we ship an
:class:`EchoSnapshotResolver` that simply echoes the query so the audit
log records something deterministic.

Two reasons we keep the resolver behind a Protocol rather than calling
the LLM inline from the API handler:

* the same handler is used by tests (which need determinism) and the
  walking skeleton (which has no LLM credentials);
* the resolver has access to both the :class:`Store` and the
  :class:`BlobStore`, so it can fetch the JSONL bytes of the target
  version without the handler caring how that data is shaped.
"""

from __future__ import annotations

from typing import Protocol

from stratoclave_atelier.blobs import BlobStore
from stratoclave_atelier.core import Version
from stratoclave_atelier.db import Store


class SnapshotResolver(Protocol):
    """Resolve a snapshot query against a frozen Version.

    Implementations may consult the :class:`Store` (e.g. fetch the
    Version row's metadata) and the :class:`BlobStore` (to read the
    canonical JSONL bytes); they MUST return a deterministic string for
    a given input or persist their non-determinism elsewhere.
    """

    async def resolve(
        self,
        *,
        store: Store,
        blob_store: BlobStore,
        version: Version,
        query: str,
    ) -> str: ...


class EchoSnapshotResolver(SnapshotResolver):
    """Reference resolver that echoes the query.

    The response includes the version's label (when set) and turn count
    so a UI rendering the audit row has something more interesting to
    show than the raw query. Used by unit tests and the walking
    skeleton; production deployments swap in an LLM-backed resolver.
    """

    async def resolve(
        self,
        *,
        store: Store,
        blob_store: BlobStore,
        version: Version,
        query: str,
    ) -> str:
        label = version.label or "<unlabeled>"
        return (
            f"[echo] version={label} turns={version.turn_count} "
            f"start_seq={version.start_seq} end_seq={version.end_seq} :: {query}"
        )
