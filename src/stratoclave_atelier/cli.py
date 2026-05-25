"""CLI entrypoint for stratoclave-atelier.

Stage A only exposes ``serve`` (uvicorn launcher) and ``migrate``
(thin shim around ``alembic upgrade``). Subsequent stages will add
``session``, ``group``, and ``version`` subcommands.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence

from stratoclave_atelier import __version__
from stratoclave_atelier.config import AtelierConfig


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

    sub.add_parser("migrate", help="Run alembic upgrade head.")
    sub.add_parser("config", help="Print effective configuration as a debug dump.")

    return parser


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    cfg = AtelierConfig.from_env()
    host = args.host or cfg.host
    port = args.port or cfg.port
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


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "serve": _cmd_serve,
        "migrate": _cmd_migrate,
        "config": _cmd_config,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
