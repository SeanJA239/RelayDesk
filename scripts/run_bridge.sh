#!/bin/sh
# Run the bridge locally with settings from .env.local.
set -eu
cd "$(dirname "$0")/.."
set -a
. ./.env.local
set +a
exec uv run uvicorn feedback_bridge.app:from_env --factory --host 0.0.0.0 --port 8787 "$@"
