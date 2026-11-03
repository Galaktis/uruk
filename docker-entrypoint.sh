#!/bin/sh
# Production start command for Uruk.
#
# Collects Django's static files (the admin — served by WhiteNoise), runs the
# self-provisioning bootstrap once (migrate all schemas + ensure the default
# catch-all tenant exists), then hands off to Granian. Run this as a single
# pre-start step — do NOT run `bootstrap` per worker, or N Granian workers
# will race on `migrate_schemas`.
set -e

# Granian tuning — production defaults, each overridable via its URUK_* variable.
# Granian maps any GRANIAN_* variable onto the matching CLI option, so options
# not covered here can still be set directly as GRANIAN_*. Worker recycling and
# respawn need Linux kernel >= 5.14 when workers > 1 (see README "Server tuning").
export GRANIAN_WORKERS="${URUK_WORKERS:-2}"
export GRANIAN_BLOCKING_THREADS="${URUK_BLOCKING_THREADS:-8}"    # per worker; keep ~= psycopg pool max_size
export GRANIAN_BACKPRESSURE="${URUK_BACKPRESSURE:-16}"           # max in-flight requests per worker
export GRANIAN_RESPAWN_FAILED_WORKERS="${URUK_RESPAWN_FAILED_WORKERS:-true}"
export GRANIAN_WORKERS_LIFETIME="${URUK_WORKERS_LIFETIME:-3600}" # seconds between periodic worker recycles
export GRANIAN_WORKERS_MAX_RSS="${URUK_WORKERS_MAX_RSS:-512}"    # MiB; recycle any worker above this

# `docker-entrypoint.sh mcp` runs the remote MCP endpoint (ASGI, /mcp on port 8001)
# instead of the website. It never migrates: start it after the website container,
# which owns bootstrap. Extra arguments are passed on to Granian.
if [ "${1:-}" = "mcp" ]; then
    shift
    # ASGI runs one blocking thread per worker; sync tools go to anyio's thread pool.
    export GRANIAN_BLOCKING_THREADS=1
    exec uv run granian --interface asgi mcp_server.http:app --port 8001 "$@"
fi

uv run python manage.py collectstatic --noinput
uv run python manage.py bootstrap
exec uv run granian --interface wsgi config.wsgi:application "$@"
