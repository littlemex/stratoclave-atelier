"""CLI entrypoint for stratoclave-atelier.

Stage A introduced ``serve`` (uvicorn launcher), ``migrate`` (thin
shim around ``alembic upgrade``), and ``config`` (effective env dump).
Stage F adds a thin ``session`` family of HTTP-backed subcommands so
operators can drive an already-running atelier instance from the
terminal:

* ``session list``           -- list sessions, optionally filtered by group.
* ``session show``           -- show one session (with versions).
* ``session send-turn``      -- append a single turn via HTTP.
* ``session freeze``         -- freeze a turn range into a Version.
* ``session fork``           -- fork a child session from a frozen Version.
* ``session snapshot-query`` -- run the cross-session RPC against a Version.
* ``session tail``           -- subscribe to the SSE event stream and print
  one JSON event per line, mirroring what the chat shell does live.

The ``--in-memory`` flag on ``serve`` no longer requires
``ATELIER_DATABASE_URL`` -- a placeholder is wired in if the variable is
unset, since the in-memory backend never opens a real connection.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from typing import Any

from stratoclave_atelier import __version__
from stratoclave_atelier.config import AtelierConfig

_DEFAULT_BASE_URL = "http://localhost:8000"
_BASE_URL_ENV = "ATELIER_BASE_URL"
_IN_MEMORY_PLACEHOLDER_URL = "postgresql+asyncpg://atelier:atelier@localhost:5432/atelier"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stratoclave-atelier",
        description="stratoclave-atelier server and admin CLI.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Start the FastAPI server with uvicorn.")
    serve.add_argument("--host", default=None, help="Override ATELIER_HOST.")
    serve.add_argument("--port", type=int, default=None, help="Override ATELIER_PORT.")
    serve.add_argument(
        "--reload",
        action="store_true",
        help="Enable uvicorn auto-reload (development only).",
    )
    serve.add_argument(
        "--in-memory",
        action="store_true",
        help=(
            "Run with the InMemoryStore + InMemoryBlobStore (no Postgres / disk). "
            "Used for the Stage E walking-skeleton demo and Playwright E2E."
        ),
    )

    sub.add_parser("migrate", help="Run alembic upgrade head.")
    sub.add_parser("config", help="Print effective configuration as a debug dump.")

    session = sub.add_parser("session", help="HTTP-backed session admin operations.")
    session.add_argument(
        "--base-url",
        default=None,
        help=(f"Atelier server base URL. Falls back to ${_BASE_URL_ENV} or {_DEFAULT_BASE_URL}."),
    )
    sess_sub = session.add_subparsers(dest="session_command", required=True)

    s_list = sess_sub.add_parser("list", help="List sessions.")
    s_list.add_argument("--group-id", default=None, help="Filter by group id (UUID).")

    s_show = sess_sub.add_parser("show", help="Show one session and its versions.")
    s_show.add_argument("session_id", help="Session UUID.")

    s_send = sess_sub.add_parser("send-turn", help="Append a single turn (HTTP).")
    s_send.add_argument("session_id", help="Session UUID.")
    s_send.add_argument("--role", default="user", help="Turn role (default: user).")
    s_send.add_argument("--content", required=True, help="Turn content as a string.")

    s_freeze = sess_sub.add_parser(
        "freeze",
        help="Freeze a turn range into an immutable Version.",
    )
    s_freeze.add_argument("session_id", help="Session UUID.")
    s_freeze.add_argument("--start-seq", type=int, default=None, help="Inclusive start seq.")
    s_freeze.add_argument("--end-seq", type=int, default=None, help="Inclusive end seq.")
    s_freeze.add_argument("--label", default=None, help="Free-form label for the Version.")

    s_fork = sess_sub.add_parser("fork", help="Fork a child session from a frozen Version.")
    s_fork.add_argument("session_id", help="Parent session UUID.")
    s_fork.add_argument("--title", required=True, help="Child session title.")
    s_fork.add_argument(
        "--parent-version-id",
        required=True,
        help="UUID of the Version to fork from.",
    )
    s_fork.add_argument(
        "--fork-seq",
        type=int,
        required=True,
        help="Turn seq inside the version where the child branches off.",
    )
    s_fork.add_argument("--group-id", default=None, help="Optional group id for the child.")

    s_snap = sess_sub.add_parser(
        "snapshot-query",
        help="Run a cross-session snapshot-query against a Version.",
    )
    s_snap.add_argument("session_id", help="Source session UUID (the asker).")
    s_snap.add_argument(
        "--target-version-id",
        required=True,
        help="UUID of the Version being asked about.",
    )
    s_snap.add_argument("--query", required=True, help="The natural-language question.")

    s_tail = sess_sub.add_parser(
        "tail",
        help="Stream the SSE event log for a session as one JSON line per event.",
    )
    s_tail.add_argument("session_id", help="Session UUID.")
    s_tail.add_argument(
        "--from-seq",
        type=int,
        default=0,
        help="Replay starting at this seq (default 0).",
    )
    s_tail.add_argument(
        "--no-follow",
        action="store_true",
        help="Drain history only, do not subscribe to live updates.",
    )

    return parser


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    if args.in_memory and not os.environ.get("ATELIER_DATABASE_URL"):
        # The in-memory store never opens a connection, so a placeholder is
        # plenty -- but AtelierConfig.from_env still requires the var to be
        # non-empty. Setting it here keeps the no-hardcode policy intact
        # because the value is documented as a placeholder, not a real URL.
        os.environ["ATELIER_DATABASE_URL"] = _IN_MEMORY_PLACEHOLDER_URL

    cfg = AtelierConfig.from_env()
    host = cfg.host if args.host is None else args.host
    port = cfg.port if args.port is None else args.port
    if args.in_memory:
        from stratoclave_atelier.blobs import InMemoryBlobStore
        from stratoclave_atelier.db import InMemoryStore
        from stratoclave_atelier.server import create_app

        os.environ.setdefault("ATELIER_IN_MEMORY", "1")
        app = create_app(cfg, store=InMemoryStore(), blob_store=InMemoryBlobStore())
        uvicorn.run(app, host=host, port=port, log_level=cfg.log_level)
    else:
        uvicorn.run(
            "stratoclave_atelier.server:app",
            host=host,
            port=port,
            log_level=cfg.log_level,
            reload=args.reload,
        )
    return 0


def _cmd_migrate(_: argparse.Namespace) -> int:
    # Alembic reads DATABASE_URL itself; we just translate the more
    # FastAPI-friendly ``ATELIER_DATABASE_URL`` (asyncpg) into the
    # psycopg variant alembic expects, if the operator only set the
    # asyncpg URL.
    if "DATABASE_URL" not in os.environ:
        url = os.environ.get("ATELIER_DATABASE_URL")
        if url and "+asyncpg" in url:
            os.environ["DATABASE_URL"] = url.replace("+asyncpg", "+psycopg")
        elif url:
            os.environ["DATABASE_URL"] = url

    from alembic import command
    from alembic.config import Config

    alembic_cfg = Config("alembic.ini")
    command.upgrade(alembic_cfg, "head")
    return 0


def _cmd_config(_: argparse.Namespace) -> int:
    cfg = AtelierConfig.from_env()
    for name in cfg.field_names():
        value = getattr(cfg, name)
        if name == "bearer_token" and value:
            value = f"<set, {len(value)} chars>"
        print(f"{name}={value}")
    return 0


def _resolve_base_url(args: argparse.Namespace) -> str:
    return args.base_url or os.environ.get(_BASE_URL_ENV) or _DEFAULT_BASE_URL


def _emit(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _request(
    method: str,
    base_url: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> Any:
    import httpx

    url = base_url.rstrip("/") + path
    with httpx.Client(timeout=30.0) as client:
        resp = client.request(method, url, json=body, params=params)
    if resp.status_code >= 400:
        try:
            detail = resp.json()
        except ValueError:
            detail = resp.text
        print(
            f"error: {method} {path} -> {resp.status_code}: {detail}",
            file=sys.stderr,
        )
        raise SystemExit(2)
    if resp.status_code == 204 or not resp.content:
        return None
    return resp.json()


def _cmd_session_list(args: argparse.Namespace) -> int:
    base = _resolve_base_url(args)
    params = {"group_id": args.group_id} if args.group_id else None
    payload = _request("GET", base, "/api/sessions", params=params)
    _emit(payload)
    return 0


def _cmd_session_show(args: argparse.Namespace) -> int:
    base = _resolve_base_url(args)
    session = _request("GET", base, f"/api/sessions/{args.session_id}")
    versions = _request("GET", base, f"/api/sessions/{args.session_id}/versions")
    _emit({"session": session, "versions": versions})
    return 0


def _cmd_session_send_turn(args: argparse.Namespace) -> int:
    base = _resolve_base_url(args)
    payload = _request(
        "POST",
        base,
        f"/api/sessions/{args.session_id}/turns",
        body={"role": args.role, "content": args.content},
    )
    _emit(payload)
    return 0


def _cmd_session_freeze(args: argparse.Namespace) -> int:
    base = _resolve_base_url(args)
    body: dict[str, Any] = {}
    if args.start_seq is not None:
        body["start_seq"] = args.start_seq
    if args.end_seq is not None:
        body["end_seq"] = args.end_seq
    if args.label is not None:
        body["label"] = args.label
    payload = _request(
        "POST",
        base,
        f"/api/sessions/{args.session_id}/freeze",
        body=body,
    )
    _emit(payload)
    return 0


def _cmd_session_fork(args: argparse.Namespace) -> int:
    base = _resolve_base_url(args)
    body: dict[str, Any] = {
        "title": args.title,
        "parent_version_id": args.parent_version_id,
        "fork_seq": args.fork_seq,
    }
    if args.group_id is not None:
        body["group_id"] = args.group_id
    payload = _request(
        "POST",
        base,
        f"/api/sessions/{args.session_id}/fork",
        body=body,
    )
    _emit(payload)
    return 0


def _cmd_session_tail(args: argparse.Namespace) -> int:
    """Stream SSE events from ``/api/sessions/{id}/events`` to stdout.

    Mirrors what the Stage F chat shell does in the browser: the SSE
    framing is decoded line-by-line, ``: ping`` keepalives are dropped,
    and each ``data:`` payload is re-emitted to stdout as a single JSON
    line so the output is pipeable into ``jq`` or a downstream process.
    Exits 0 on a clean stream end (history-only mode) or on Ctrl-C.
    """

    import httpx

    base = _resolve_base_url(args)
    follow = not args.no_follow
    url = base.rstrip("/") + f"/api/sessions/{args.session_id}/events"
    params = {"from_seq": args.from_seq, "follow": "true" if follow else "false"}

    timeout = httpx.Timeout(10.0, read=None)
    try:
        with (
            httpx.Client(timeout=timeout) as client,
            client.stream("GET", url, params=params) as resp,
        ):
            if resp.status_code >= 400:
                detail: Any
                try:
                    detail = resp.read().decode("utf-8", errors="replace")
                except Exception:
                    detail = "<unreadable>"
                print(
                    f"error: GET {url} -> {resp.status_code}: {detail}",
                    file=sys.stderr,
                )
                return 2

            for raw_line in resp.iter_lines():
                line = raw_line.rstrip("\r")
                if not line or line.startswith(":"):
                    continue
                if line.startswith("data:"):
                    data = line[len("data:") :].lstrip()
                    if not data:
                        continue
                    try:
                        parsed = json.loads(data)
                    except ValueError:
                        # Surface unparseable frames raw rather than silently dropping.
                        print(data)
                        sys.stdout.flush()
                        continue
                    print(json.dumps(parsed, sort_keys=True, default=str))
                    sys.stdout.flush()
    except KeyboardInterrupt:
        return 0
    return 0


def _cmd_session_snapshot_query(args: argparse.Namespace) -> int:
    base = _resolve_base_url(args)
    payload = _request(
        "POST",
        base,
        f"/api/sessions/{args.session_id}/snapshot-query",
        body={"target_version_id": args.target_version_id, "query": args.query},
    )
    _emit(payload)
    return 0


def _cmd_session(args: argparse.Namespace) -> int:
    handlers = {
        "list": _cmd_session_list,
        "show": _cmd_session_show,
        "send-turn": _cmd_session_send_turn,
        "freeze": _cmd_session_freeze,
        "fork": _cmd_session_fork,
        "snapshot-query": _cmd_session_snapshot_query,
        "tail": _cmd_session_tail,
    }
    return handlers[args.session_command](args)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "serve": _cmd_serve,
        "migrate": _cmd_migrate,
        "config": _cmd_config,
        "session": _cmd_session,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
