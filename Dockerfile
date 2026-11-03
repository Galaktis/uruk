# Uruk application image: the single, self-provisioning app container.
#
# On start, docker-entrypoint.sh collects static files, runs `manage.py bootstrap`
# (migrate every schema + seed the default site) once, then hands off to Granian.
# Postgres is the only required backing service; see docker-compose.yml.
FROM python:3.15-slim

COPY --from=ghcr.io/astral-sh/uv:0.13 /uv /uvx /bin/

# Set by the release workflow from the git tag; "dev" for local builds.
ARG VERSION=dev

LABEL org.opencontainers.image.title="Uruk" \
      org.opencontainers.image.description="A self-hosted, multi-tenant CMS built for AI agents, where a website is data, not code" \
      org.opencontainers.image.url="https://uruk.galaktis.eu" \
      org.opencontainers.image.source="https://github.com/Galaktis/uruk" \
      org.opencontainers.image.documentation="https://github.com/Galaktis/uruk#readme" \
      org.opencontainers.image.vendor="Galaktis" \
      org.opencontainers.image.licenses="BSD-2-Clause" \
      org.opencontainers.image.version="${VERSION}"

ENV PYTHONUNBUFFERED=1 \
    # The source is read-only for the runtime user; bytecode is compiled at build time.
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    # The venv is built at image time; `uv run` must not try to re-sync it (that
    # would pull the dev group back in at container start).
    UV_NO_SYNC=1 \
    UV_CACHE_DIR=/tmp/uv-cache \
    # Granian binds loopback by default — unreachable from outside the container.
    GRANIAN_HOST=0.0.0.0 \
    GRANIAN_PORT=8000

# An unprivileged runtime user. A fixed uid lets a mounted volume (for example the
# URUK_STORAGE_ROOT of the filesystem asset backend) be made writable with `chown 10001`.
RUN useradd --system --uid 10001 --gid 0 --no-create-home --home-dir /app uruk

WORKDIR /app

# Dependencies first, so source edits don't invalidate the (slow) dependency layer.
# The uv cache lives in a BuildKit cache mount, so it never ends up in the image.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    UV_CACHE_DIR=/root/.cache/uv uv sync --frozen --no-dev --no-install-project

COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    UV_CACHE_DIR=/root/.cache/uv uv sync --frozen --no-dev \
    && chmod +x docker-entrypoint.sh \
    # collectstatic writes here at start; everything else stays root-owned and read-only.
    && mkdir -p staticfiles && chown uruk:0 staticfiles

USER uruk
EXPOSE 8000
ENTRYPOINT ["./docker-entrypoint.sh"]
