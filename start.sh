#!/bin/sh
set -eu
exec python -m uvicorn web.app:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}" --workers 1 --no-access-log --no-proxy-headers --limit-concurrency 32
