#!/usr/bin/env bash
#
# Run the test suite against a clean, disposable Paperless-ngx.
#
# Flow: remove the dev stack and its volumes -> build & start it (Paperless
# boots, the seed fills it, the MCP server starts) -> wait for MCP health ->
# run pytest -> remove the stack and its volumes again.
#
# The instance lives entirely in named volumes, so `down -v` is the reset.
# The final teardown runs via an EXIT trap, so it happens even if a test fails.
#
# Any extra arguments are passed through to pytest, e.g.:
#   ./run-tests.sh                       # full suite
#   ./run-tests.sh tests/live -q         # just the live tests, quietly
#   ./run-tests.sh -k upload             # a single test by keyword
#
# Set KEEP_STACK=1 to leave the stack running afterwards.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

wait_for_health() {
  local url="http://localhost:9191/health"
  echo "Waiting for MCP health at $url ..."
  for _ in $(seq 1 60); do
    if [ "$(curl -fs "$url" 2>/dev/null || true)" = "ok" ]; then
      echo "MCP is healthy."
      return 0
    fi
    sleep 1
  done
  echo "ERROR: MCP did not become healthy within 60s." >&2
  return 1
}

run_pytest() {
  if command -v uv >/dev/null 2>&1; then
    ( cd app && uv run pytest "$@" )
  else
    # No uv on this machine: run the tests in uv's own image instead, on the
    # host network so localhost:9190/9191 are the dev stack.
    docker run --rm --network host -v "$REPO_ROOT/app:/app" -w /app \
      -e UV_PROJECT_ENVIRONMENT=/tmp/venv -e UV_CACHE_DIR=/tmp/uv-cache \
      ghcr.io/astral-sh/uv:python3.12-bookworm-slim uv run pytest "$@"
  fi
}

cleanup() {
  if [ -n "${KEEP_STACK:-}" ]; then
    echo "== KEEP_STACK set: leaving the stack running =="
    return
  fi
  echo "== Teardown: removing containers and volumes =="
  docker compose down -v
}
# Always tear down on exit, even if the tests fail.
trap cleanup EXIT

echo "== Removing any previous stack =="
docker compose down -v

echo "== Building and starting containers =="
# --build so the MCP server reflects the current source. The MCP container
# starts only after the seed has finished, so its health means "seeded".
if ! docker compose up -d --build; then
  # `up` names only the service that failed; its own output says why.
  docker compose logs --no-color --tail 40 seed paperless
  exit 1
fi

wait_for_health

echo "== Running tests =="
run_pytest "$@"
