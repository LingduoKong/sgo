#!/bin/sh
# Explicit initialization only. Re-running restores the specified users if revoked.
set -eu
: "${SGO_ADMIN_EMAIL:?Set SGO_ADMIN_EMAIL to the administrator email}"
docker compose exec -T app python -m web.auth allow "$SGO_ADMIN_EMAIL" --admin
if [ -n "${SGO_MEMBER_EMAIL:-}" ]; then
  docker compose exec -T app python -m web.auth allow "$SGO_MEMBER_EMAIL"
fi
