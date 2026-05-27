#!/usr/bin/env bash
# Local dev launcher for stratoclave-atelier.
#
# Picks a container engine via $ATELIER_CONTAINER_ENGINE (default: auto-detect
# docker > finch). Brings up the Postgres + pgvector compose service, runs
# alembic migrations, then exec's uvicorn so the server takes over PID 1
# (Ctrl-C cleanly stops it; the postgres container keeps running).
#
# Usage:
#   ATELIER_CONTAINER_ENGINE=finch ./scripts/dev-up.sh                    # serve on :8000 with Postgres
#   ATELIER_CONTAINER_ENGINE=docker ./scripts/dev-up.sh
#   ATELIER_CONTAINER_ENGINE=none ./scripts/dev-up.sh --in-memory --port 8123
#                                                                        # skip Postgres entirely
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${ROOT}"

ENGINE="${ATELIER_CONTAINER_ENGINE:-}"
if [[ -z "${ENGINE}" ]]; then
  if command -v docker >/dev/null 2>&1; then
    ENGINE=docker
  elif command -v finch >/dev/null 2>&1; then
    ENGINE=finch
  else
    ENGINE=none
  fi
fi

# Pass-through serve flags. Defaults to a Postgres-backed serve on :8000
# unless the caller asks for --in-memory.
SERVE_ARGS=("$@")
WANT_INMEMORY=0
for a in "${SERVE_ARGS[@]:-}"; do
  if [[ "${a}" == "--in-memory" ]]; then
    WANT_INMEMORY=1
  fi
done

if [[ "${ENGINE}" == "none" || "${WANT_INMEMORY}" == "1" ]]; then
  echo "[dev-up] engine=${ENGINE} (skipping Postgres)"
else
  case "${ENGINE}" in
    docker)
      echo "[dev-up] engine=docker"
      docker compose up -d
      ;;
    finch)
      echo "[dev-up] engine=finch"
      if ! finch vm status 2>/dev/null | grep -q Running; then
        echo "[dev-up] starting finch VM"
        finch vm start
      fi
      finch compose up -d
      ;;
    *)
      echo "[dev-up] unknown ATELIER_CONTAINER_ENGINE=${ENGINE} (expected docker / finch / none)" >&2
      exit 2
      ;;
  esac

  export DATABASE_URL="${DATABASE_URL:-postgresql+psycopg://atelier:atelier@localhost:5432/atelier}"
  export ATELIER_DATABASE_URL="${ATELIER_DATABASE_URL:-postgresql+asyncpg://atelier:atelier@localhost:5432/atelier}"

  echo "[dev-up] waiting for Postgres on localhost:5432..."
  for i in $(seq 1 30); do
    if (echo > /dev/tcp/127.0.0.1/5432) >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done

  echo "[dev-up] alembic upgrade head"
  alembic upgrade head
fi

echo "[dev-up] launching server: ${SERVE_ARGS[*]:-(defaults)}"
exec stratoclave-atelier serve "${SERVE_ARGS[@]}"
