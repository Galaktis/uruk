# AGENTS.md

Guidance for AI coding agents (Claude Code, Codex, Cursor, ...) working in this repository.

Machine-specific notes (deployment details) live in a gitignored `AGENTS.local.md`, imported
here when it exists:

@AGENTS.local.md

## What Uruk Is

Uruk is a **multi-tenant CMS where a website is data, not code**. A single deployment
serves many sites. Each site is a **tenant** with its own Postgres schema (`django-tenants`);
an incoming request's `Host` header is resolved to a `Domain`, which selects the tenant, and
the connection's `search_path` is switched to that tenant's schema for the rest of the request.

A site is composed at runtime from a tree of reusable **Element** rows, rendered to HTML,
cached, and served. There are **no per-site templates and no commit-to-create**: the only
committed code is a library of *generic* element renderers; every actual site is data in the
database. Administration is API-first, and the **first client is an MCP server** — an AI agent
builds and edits sites before any human UI exists.

`ARCHITECTURE.md` is the authoritative design document (vision, data model, rendering,
caching, tenancy, MCP, and the §14 phased roadmap). Read it before making structural changes.

## Commands

Dependencies are managed with **uv**. Requires Python 3.15, uv, and Docker (for Postgres/Redis).

```bash
docker compose up -d postgres redis   # backing services only (tests need Postgres up); = `just up`
uv sync                            # install/refresh runtime + dev deps

uv run python manage.py migrate            # apply migrations (public + tenant schemas)
uv run python manage.py makemigrations
uv run python manage.py runserver          # local dev server

uv run pytest                              # run the full suite
uv run pytest tests/test_x.py::test_y      # run a single test
uv run coverage run -m pytest && uv run coverage report

uv run ruff check .                # lint (ruff "ALL" ruleset, line-length 160)
uv run ruff check --fix .          # autofix
uv run ruff format .               # format
uv run ty check                    # type-check (Astral ty + django-stubs, no suppressions)
```

Real Postgres is required — there is no SQLite fallback (`django-tenants` needs schemas).
A bare `docker compose up` also builds and starts the `app` service (the `Dockerfile` image) on
http://localhost:8000 — the full self-provisioning stack; CI smoke-tests exactly that. CI lives in
`.github/workflows/ci.yml` (lint + ty, Postgres test suite, fresh-compose smoke test).
Production serving is **Granian** (WSGI) against `config.wsgi:application`, not `runserver`.

### Run the MCP server

The admin surface is exposed to an AI agent over MCP. It authenticates with a per-tenant
API token (`content.ApiToken`) that resolves to a Django user in the tenant schema; the agent can
do exactly what that user's permissions allow. Issue a token with `manage.py create_api_token`:

```bash
uv run python manage.py create_api_token <schema_name> <username>   # prints the token once
URUK_MCP_TENANT=<schema_name> URUK_MCP_TOKEN=<token> uv run python -m mcp_server   # stdio
uv run granian --interface asgi mcp_server.http:app --port 8001                     # HTTP (/mcp)
```

Two transports, one tool set (`mcp_server/server.py`). Every tool takes the SDK `Context`
and calls `_run(ctx, tools.<name>, ...)`: over HTTP the per-request `Host` (tenant) and
bearer token build the `AgentContext`, over stdio the env does. The HTTP app
(`mcp_server/http.py`) is stateless, rejects bad tokens with 401 before MCP runs, and is
served by Granian ASGI as the compose `mcp` service (`docker-entrypoint.sh mcp`). Sync tools
run in anyio worker threads, so they close their DB connection when done.

### Releasing

Pushing a `vX.Y.Z` tag runs `.github/workflows/release.yml`: the full CI gate (reused via
`workflow_call`), then a multi-arch (amd64 + arm64) image pushed to Docker Hub
(`galaktis/uruk`) and `ghcr.io/galaktis/uruk` as `X.Y.Z`, `X.Y` and `latest`. It only runs
in the `Galaktis/uruk` repository. Bump `version` in `pyproject.toml` before tagging. The
image runs as the unprivileged uid 10001 with a read-only `/app` (only `staticfiles/` is
writable). Compose services carry both `image:` and `build:`: `up` pulls
the release, `up --build` builds the checkout.

## Configuration

All configuration is environment-driven (no committed `.env`); settings live in `config/settings.py`
reading `URUK_*` variables. The full variable reference is in README.md "Configuration" (mirrored
by `docker-compose.yml` comments and the default site's `/configuration` page — keep all three in
sync when adding a variable). Key behavioral switches:

- `URUK_DEBUG` — when `False` (default), production behaviours apply.
- `URUK_DB_POOL` — Django 6 native psycopg3 connection pooling (default on); no external
  pooler needed.
- Cache backend is chosen by environment: **Redis** when `URUK_REDIS_URL` is set, else **Postgres**
  (`DatabaseCache` over the shared `uruk_cache` table in the public schema). Every cache key is
  prefixed with the active tenant schema (`caching.keys.tenant_key_function`) so cached output
  never leaks across tenants.
- Granian tuning: `URUK_WORKERS`, `URUK_BLOCKING_THREADS`, `URUK_BACKPRESSURE`,
  `URUK_RESPAWN_FAILED_WORKERS`, `URUK_WORKERS_LIFETIME`, `URUK_WORKERS_MAX_RSS` — forwarded by
  `docker-entrypoint.sh` as `GRANIAN_*` options with production defaults (README "Server tuning").
- OpenTelemetry uses the **standard `OTEL_*` variables** (the one family not renamed to `URUK_*`).
  `OTEL_EXPORTER_OTLP_ENDPOINT` is the gate: unset, the SDK is never imported
  (`config/telemetry.py`); set, requests/psycopg/redis are traced and every request span carries
  the tenant schema as `uruk.tenant`.
- `SECRET_KEY` comes from `URUK_SECRET_KEY`.

## Architecture

The project is split into focused modules (see `ARCHITECTURE.md §13`). Every model has a
**UUIDv7 primary key** (`common.models.UUID7Model`, using Python 3.14's stdlib `uuid.uuid7()`)
and `created`/`updated` timestamps (`TimestampedModel`).

```
config/       # settings, urls, wsgi/asgi
common/       # UUID7Model / TimestampedModel base classes, validators, typed tenancy/user helpers
tenants/      # SHARED (public schema): Tenant, Domain
content/      # TENANT schema: Page, Element, Asset, Setting, PageDependency + serving views
services/     # shared service layer (content ops, portability) — the single source of truth
rendering/    # renderer registry + built-in block kinds + RenderContext
caching/      # computed-page/fragment cache + dependency-graph invalidation
api/          # django-ninja routers + schemas, authorised by Django permissions
mcp_server/   # MCP server: tools over the service layer (named to avoid the `mcp` SDK clash)
studio/       # /_studio/: htmx review UI for humans (Django templates, vendored htmx)
tests/        # outside-in integration suite
```

### The default website

A fresh deployment serves a complete website on any hostname: the default (catch-all) tenant
is seeded on first start from `tenants/seed/default_site.json` — a §10 portability archive
committed with the repo. That site is authored in `scripts/default_site/` (CSS, JS, SVG, HTML
sections, fonts) and composed by `scripts/build_default_site.py`; after editing any source,
regenerate the archive:

```bash
uv run python scripts/build_default_site.py    # rewrites tenants/seed/default_site.json
```

`manage.py demo [--reset]` (`just demo`, against a running compose stack) is the end-to-end
showcase: it provisions a `demo` tenant on `demo.localhost` and builds a blog by calling the
remote MCP endpoint over HTTP like a hosted agent (`post_json` is the swappable transport in
tests). Keep it working when changing tools — `tests/test_demo.py` drives it in-process.

Seeding happens once per tenant (skipped if any Page exists), via
`services.portability.restore_content` — the same code path as archive restore.

### Tenancy model

One Postgres database. `SHARED_APPS` (public schema) hold tenant routing (`tenants.Tenant`,
`tenants.Domain`); `TENANT_APPS` are migrated into every tenant's own schema. Auth lives in
`TENANT_APPS` — **users and permissions are per-tenant**, fully isolated by schema. There is no
cross-tenant identity by default; a separate platform admin manages tenant lifecycle.

`django_tenants.middleware.main.TenantMainMiddleware` runs first on every request and switches
the connection to the matching tenant's schema. In tests, drive requests with a `Host` header
matching a `Domain`, and use `django_tenants.utils.tenant_context(tenant)` to run ORM code in a
tenant schema directly.

### Data model (`content/models.py`)

- **Element** — a node in a page's composition tree: a `kind` (selects the renderer),
  `parameters` (JSON) and `content` (text/HTML), self-nesting via `parent`/`position`.
  Headers, footers, and page bodies are all Elements; `is_reusable` marks shared ones.
- **Page** — a node in the URL tree: `slug`, `name`, self-nesting via `parent`/`position`
  (unique `(parent, slug)`), and a `root_element` FK it renders from.
- **Asset** — a binary/text asset (JS/CSS/image) stored in the DB, with a `sha256` content hash
  recomputed on save, served from immutable content-hashed URLs (`§9`).
- **Setting** — a namespaced, DB-stored configuration entry (`key` → JSON `value`).
- **Revision** — an immutable published snapshot (JSON: elements, pages, settings) of the site.
  The Element/Page/Setting rows are the *working copy*; the public site serves the latest
  Revision (draft mode), or the working copy directly if the site was never published (live
  mode). `services/publishing.py` owns publish/rollback/discard/diff (`ARCHITECTURE.md §16`).
- **PageDependency** — the reverse index of which elements a page's cached output depends on
  (`§7`). `element_id` is a bare UUID (not a FK) so the row survives element deletion long
  enough for the `post_delete` signal to find and evict the affected pages.

### Rendering & caching (the core mechanism)

`rendering/registry.py` maps each Element `kind` to a generic committed renderer via `@register`;
`render_element` walks the tree recursively and records every touched element on the
`RenderContext` (the dependency set). Built-in kinds live in `rendering/blocks.py`
(`container`, `text`, `heading`, `image`, `link`, `list`/`list_item`, `nav`, `raw_html`,
`reference`, `page_list`) — editor text is escaped with
`format_html`; `raw_html` is the single explicit trusted-HTML escape hatch. `reference`
renders another element's subtree in place (`parameters.element_id`) — the include mechanism
that lets one reusable header/footer serve many pages; it records dependencies through the
include, and cycles are cut, not recursed. A page's `<head>` is data too: `render_document`
(`rendering/document.py`) injects the `site.head_html` setting plus the page's `head_html` parameter.

`caching/service.py` caches computed page/fragment HTML and persists the dependency set as
`PageDependency` rows. Invalidation (`caching/signals.py`) fires on element `post_save`/
`post_delete`: it evicts the pages that used the element plus the fragments rooted at it and
each ancestor. **Getting invalidation right is the engineering core** — change it carefully.

Renderers read content only through `ctx.source` (`rendering/sources.py`): `DatabaseSource` for
the working copy, `SnapshotSource` for a published revision — never query Element/Page/Setting
directly from a renderer, or published pages would leak draft content. Published output is
cached under revision-scoped keys with no invalidation (a revision is immutable); the §7
dependency graph serves the working copy (live mode and `/_preview/`).

**Collections.** A collection is a page's child pages (a blog: posts with `parameters.date`,
`summary`, `tags`). `page_list` lists them and records the *parent page id* via
`ctx.record_collection` — page ids share the `PageDependency.element_id` column with element
ids — so the Page signals (`pre_save` remembers the old parent for moves) evict every listing
when a child is added/edited/moved/deleted. The same children feed `<path>/feed.xml` (RSS);
`/sitemap.xml` and `/robots.txt` (overridable via the `site.robots_txt` setting) are served
from the public source (live revision, or the working copy in live mode).

### Service layer, API, and MCP

`services/` is the single home for content operations; both `api/` (django-ninja) and
`mcp_server/` call it — no logic is duplicated. Authorisation is Django's permission system
everywhere; the MCP server authenticates as a tenant-scoped Django user and its tools expose
exactly that user's permissions.

### Studio (`studio/`)

The human review UI at `/_studio/` — Django templates + vendored htmx (`studio/static/studio/`,
SRI-pinned; bump the `integrity` attribute in `studio.html` with the file). Views are thin
wrappers over `services`, each guarded by `studio_view(<perm>)` (session login via the admin, mounted at `/<URUK_ADMIN_PATH>/`, default `/uruk/`).
Mutating views return the refreshed fragment with `HX-Trigger: content-changed`; the status
bar, tree, and the framed `/_preview/` (served `X-Frame-Options: SAMEORIGIN`) listen for it.

## Conventions

- Ruff runs with the `ALL` ruleset (line length 160); migrations are excluded. **Every module,
  class, and function has a docstring** — match that style. `# noqa` gets a rationale comment.
- Modules use `from __future__ import annotations` and put type-only imports under
  `if TYPE_CHECKING:`.
- `validate_lowercase` (`common/validators.py`) enforces lowercase slugs.
- Typing: ty runs over everything (tests included) with django-stubs and no rule
  suppressions. Reach the tenant connection through `common.tenancy`
  (`current_schema()`, `tenant_connection()`) and the request user through `common.users`
  (`request_user`, `authenticated_user`); declare reverse relations under `if TYPE_CHECKING:`
  on the model; use `require_http_methods([...])`, not `require_GET`/`require_POST` (their
  stubs don't type-check under ty).
- Model FKs use string references (`"content.Element"`, `"content.Page"`) and explicit
  `related_name`s.
- Tests are **outside-in**: drive the service/API/HTTP surface, then assert on tenant DB state
  and rendered HTML. Tenant isolation is tested explicitly, including under the connection pool.
