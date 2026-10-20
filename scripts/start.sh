#!/bin/sh
# Start the dot-gateway MCP server (foreground). Extra flags are passed through,
# e.g. scripts/start.sh --port 8765 --log-level DEBUG
# Uses absolute paths so it also works under launchd (bare PATH).
set -eu
REPO="$(cd "$(dirname "$0")/.." && pwd)"
UV="${UV:-$HOME/.local/bin/uv}"
[ -x "$UV" ] || UV="$(command -v uv)"
exec "$UV" run --project "$REPO" --frozen --no-dev dot-gateway "$@"
