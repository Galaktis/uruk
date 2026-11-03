# Uruk — Architecture & Design

> How Uruk works and why: the design behind the code.

## 1. Vision

Uruk is a **multi-tenant CMS where a website is data, not code**. A site is composed
at runtime from a tree of reusable **Elements**, rendered to HTML, cached, and served.
Creating or changing a site never requires committing a template or deploying code —
the entire definition lives in the database and is driven through APIs.

Positioning, in one line:

> *A block model composable and deployable entirely at runtime from the DB — full-site
> editing without committing templates.*

### The defining constraint

Developer-first frameworks have a developer define page types in code and write HTML
templates that are **committed to the repo**; editors only fill in fields. Uruk's defining
constraint is the opposite — **no per-site templates, no commit-to-create**. The only
committed code is a library of *generic* element renderers; every actual site is data.

## 2. Goals & non-goals

**Goals**
- Pages built from a tree of Elements, composed at runtime.
- Computed pages/fragments cached (DB or Redis) with correct, dependency-aware invalidation.
- Multi-tenant via **schema-per-tenant** (`django-tenants`), one Postgres.
- Administered **entirely through APIs**, authorised **entirely by Django's permission system**.
- An **MCP server** over those APIs so an AI agent can build and edit sites directly —
  shipped *before* any human UI.
- Maximum configuration stored **in the database**, not in settings files.
- Minimal use of the Django template engine; minimal files on disk (JS/CSS live in the DB).
- htmx as the dynamic-rendering mechanism.
- Portable sites: a tenant can be archived and restored on another Uruk instance fearlessly.

**Non-goals (for the PoC)**
- Sharding (horizontal split across DB servers). Schema-per-tenant is *not* sharding and is
  sufficient for a long time. Revisit only when one Postgres can't hold the fleet.
- A visual page builder UI. The PoC ships APIs + htmx-rendered output, plus a review UI
  (the Studio); a drag-and-drop builder is later.
- A plugin ecosystem. We target developers/agencies running many sites.

## 3. Technology stack

| Concern            | Choice                          | Notes / rationale |
|--------------------|---------------------------------|-------------------|
| Language           | **Python 3.15**                 | stdlib `uuid.uuid7()` is built-in (see §10). |
| Framework          | **Django 6.x**                  | Native psycopg3 **connection pooling** — no external pooler needed. |
| DB                 | **PostgreSQL** (psycopg 3)      | Required by `django-tenants` (schemas). No SQLite. |
| Tenancy            | **django-tenants**              | Schema-per-tenant + host routing. |
| API                | **django-ninja**                | Type-hint/Pydantic-driven — pairs naturally with the "annotate everything" goal; async-capable. |
| Agent interface    | **MCP server** (Python SDK)     | Exposes the admin API as tools so an AI agent can operate Uruk before any UI exists (§11). |
| Cache / sessions   | **Redis** (django-redis), DB fallback | Output cache + tag invalidation. |
| Server             | **Granian** (WSGI/ASGI)         | Tuned via `URUK_*` → `GRANIAN_*` forwarding in `docker-entrypoint.sh`. |
| Edge / TLS         | **Caddy** *(planned)*           | Reverse proxy for per-tenant TLS — see the note below. |
| Observability      | **OpenTelemetry**               | Env-var driven, loaded **only when configured** — see the note below. |
| Dynamic UI         | **htmx**                        | Server renders fragments; htmx swaps them. |
| Lint + format      | **Ruff** (linter **and** formatter) | One tool for both; no other formatter. |
| Types              | **ty** + systematic annotations | Astral's type checker. It is young (preview) — keep mypy as a fallback if `ty` blocks us. |
| Tests              | **pytest / pytest-django / factory-boy** | Outside-in (see §11). |

**Sync, not async.** The admin API and the MCP-facing surface are built sync.
`django-tenants` isolates tenants via the connection's `search_path` through Django's
thread-local connection handling; async views run the ORM in a thread executor where
that per-connection state is fragile and risks cross-tenant leakage (the §3 risk #2 we
proved doesn't happen). The workload is also short DB-bound CRUD with no async upside,
the service layer is sync, and we serve WSGI. Escape hatch: a genuinely I/O-bound,
*non-tenant-ORM* endpoint may be async in isolation, but the tenant-ORM core stays sync.

**Caddy in front (planned).** Granian needs no proxy for the classic reasons — its Rust HTTP
layer already shields workers from slow clients — but production wants a thin edge for one
Uruk-specific problem: **per-tenant TLS**. Tenants bring their own domains (`tenants.Domain`),
and Caddy's on-demand TLS issues a certificate at the first handshake for a new hostname,
gated by an `ask` endpoint: a tiny public view answering 200 iff the hostname exists in the
`Domain` table. Adding a domain then gives it HTTPS with zero operator action — the "website
is data" idea extended to certificates (nginx would need certbot, per-domain config, and
reloads). Caddy also brings response compression (Granian's WSGI mode doesn't compress) and a
hygiene layer (request size caps, real client IPs). Two rules when it lands: it must forward
`Host` unchanged — that header is the tenant router — and it must **not** proxy-cache pages:
§7's dependency-graph invalidation owns caching, and an outer HTTP cache would serve stale
HTML it can't evict (and is a cross-tenant leak vector if not keyed by host). If outer-edge
caching is ever needed, it's a CDN with purging wired into §7's eviction, not a proxy cache.

**OpenTelemetry.** Traces follow the same rule as every other backend choice
(cache, storage): **the environment decides, and nothing loads unless configured.** Configuration
uses the standard `OTEL_*` variables (endpoint, headers, service name, sampling) rather than
inventing `URUK_*` equivalents; the gate is `OTEL_EXPORTER_OTLP_ENDPOINT` — when it is unset,
the SDK is never imported or initialised and a plain deployment pays zero overhead. When set,
`config.telemetry.configure_telemetry()` (called per process by `config.wsgi` and `manage.py`,
so each Granian worker initialises post-fork) wires the OTel SDK plus Django/psycopg/redis
instrumentation and trace-id log correlation. The OTel packages ship as regular dependencies —
**one build does everything**; activation is purely environmental. One Uruk-specific
requirement: every request span carries the resolved tenant `schema_name` (attribute
`uruk.tenant`, stamped by the Django instrumentation's response hook after the tenant
middleware has run), so traces can be filtered per tenant.

**Compatibility risks to verify before committing (in order):**
1. `django-tenants` has a release supporting **Django 6 / Python 3.14**.
2. Connection pooling + `django-tenants`: pooled connections retain `search_path`.
   `django-tenants` sets the schema per request, but verify it always re-sets on a checked-out
   pooled connection, and decide on `CONN_MAX_AGE`. **Write an explicit tenant-isolation test
   that runs under the pool.**
3. `ty`'s maturity against a Django codebase (stubs for Django/ninja).

## 4. Tenancy model (django-tenants)

One Postgres database. The **public schema** holds the tenant routing; each tenant gets its
**own schema** with its own content, users, and permissions.

```python
SHARED_APPS = [
    "django_tenants",
    "tenants",                       # Tenant, Domain  (public schema)
    "django.contrib.contenttypes",
    "django.contrib.staticfiles",
]
TENANT_APPS = [
    "django.contrib.auth",           # per-tenant users + permissions
    "django.contrib.admin",
    "django.contrib.sessions",
    "content",                       # Page, Element, Asset, Setting
    "api",
]
INSTALLED_APPS = list(SHARED_APPS) + [a for a in TENANT_APPS if a not in SHARED_APPS]

TENANT_MODEL = "tenants.Tenant"
TENANT_DOMAIN_MODEL = "tenants.Domain"
DATABASE_ROUTERS = ("django_tenants.routers.TenantSyncRouter",)
# DATABASES["default"]["ENGINE"] = "django_tenants.postgresql_backend"
# MIDDLEWARE starts with "django_tenants.middleware.main.TenantMainMiddleware"
```

**Key decision — auth lives in `TENANT_APPS` (per-tenant users).** Each site administers its
own users and permissions with stock Django auth, fully isolated by schema. This is exactly
what "rely entirely on Django's rights management" wants. The price: there is no single user
identity spanning tenants. A separate **platform admin** (operating in the public schema)
manages tenant lifecycle (create/delete/archive/restore). If we later want one identity across many
sites, that's a deliberate change to shared auth, not the default.

`Tenant` (with `schema_name`) is the site;
`Domain` maps a hostname to a tenant; the middleware is `django-tenants`'.

**The default tenant ships with a real website.** Any hostname without a matching `Domain`
falls back to one catch-all default tenant, created on first start (or first request) and
seeded from `tenants/seed/default_site.json` — a committed §10 portability archive holding a
complete site: pages, element trees, a shared header/footer included by `reference`, and
CSS/JS/font assets. It is authored in `scripts/default_site/` and rebuilt with
`scripts/build_default_site.py`; seeding reuses `restore_content`, the same code path as
archive restore, and is skipped the moment the tenant has any page of its own.

**Pod startup is self-provisioning.** When a pod boots, `docker-entrypoint.sh` runs
`manage.py bootstrap` **before** Granian starts: it applies migrations to the public schema and
every tenant schema (`migrate_schemas`), then creates and seeds the default tenant iff it does
not already exist. The command is idempotent — safe on every boot — so a fresh deployment serves
the complete default website with no manual step, and an upgraded pod comes up already migrated.
It must run as a single pre-start step, never per worker (N workers would race on
`migrate_schemas`). The first superuser is deliberately *not* part of this path — it is claimed
on demand via `POST /api/bootstrap`, keeping credentials out of startup. Because that
endpoint hands its set-password link to the caller, it requires the tenant's *claim code*: an
HMAC of the schema name under `SECRET_KEY`, printed by `bootstrap` while the tenant is unclaimed
and by `manage.py claim_code`. Reaching an unclaimed site first is not enough to take it over.

## 5. Data model

### Public schema (`tenants`)
- **Tenant** — `schema_name`, name, status, `parameters` (JSON), timestamps.
- **Domain** — hostname → Tenant; primary flag; the `*` catch-all behaviour can be a fallback domain.

### Tenant schema (`content`)
- **Page** — `slug`, `name`, `parent`/`position` (self-nesting → URL tree), `root_element` FK, `parameters`.
- **Element** — `kind`, `parent`/`position` (self-nesting → the composition tree), `parameters` (JSON),
  `content` (text), optional reusability flag. Headers, footers, and page bodies are all Elements.
- **Asset** — `name`, `content_type`, `bytes`/`text`, `sha256` (content hash). JS/CSS/images-in-DB (§9).
- **Setting** — `key`, `value` (JSON), scope. DB-stored configuration (§8).
- **Revision** — `number`, `message`, `author`, `published_at`, `data` (JSON snapshot of elements,
  pages, settings). An immutable published version of the site; the latest one is what visitors see (§16).

**Every model uses a UUIDv7 primary key** via an abstract base (§10). Foreign keys use string
references and explicit `related_name`s. Slugs keep the `lowercase_validator`.

## 6. Rendering engine — composing pages without committed templates

This is the mechanism that makes "a website is data" true.

A **renderer registry** maps an Element `kind` to a Python callable. Renderers are *generic,
committed once* — they are the only templates in the system, and they describe block *types*
(text, container, grid, image, nav, raw-html, …), never specific sites.

```python
# rendering/registry.py
from collections.abc import Callable
from django.utils.safestring import SafeString, mark_safe

Renderer = Callable[["Element", list[SafeString], "RenderContext"], SafeString]
RENDERERS: dict[str, Renderer] = {}

def register(kind: str) -> Callable[[Renderer], Renderer]:
    def deco(fn: Renderer) -> Renderer:
        RENDERERS[kind] = fn
        return fn
    return deco

def render_element(element: "Element", ctx: "RenderContext") -> SafeString:
    children = [render_element(c, ctx) for c in element.ordered_children()]
    ctx.record_dependency(element)          # feeds the invalidation graph (§7)
    return RENDERERS[element.kind](element, children, ctx)
```

```python
@register("container")
def _container(el, children, ctx) -> SafeString:
    attrs = el.parameters.get("attrs", "")
    return mark_safe(f"<div {attrs}>{''.join(children)}</div>")  # children pre-escaped
```

A page renders by walking its `root_element` tree. Data comes from `element.parameters`
(structured) and `element.content` (free text/HTML). **Editor-supplied HTML/text is escaped or
sanitised** in the renderer; HTML is never built by raw string concatenation.

**Reuse is a block kind, not a tree exception.** The element tree is strictly single-parent;
sharing happens through the `reference` kind, which renders another element's subtree in place
(`parameters.element_id`). One header element serves every page that references it. The include
renders through the same `RenderContext`, so dependencies are recorded *through* the reference
and invalidation stays exact (§7); dangling targets render as nothing and cycles are cut. The
`<head>` is composed the same data-driven way: the `site.head_html` Setting plus a per-page
`head_html` parameter are injected by the document wrapper — trusted editor HTML, the head-side
counterpart of `raw_html`.

**Why this beats "use Django templates":** because computed output is cached (§7), per-render
cost is amortised to near-zero, so the renderer optimises for *correctness and runtime
composability*, not template-engine speed. A site needs zero committed templates; only new
*block kinds* need code, and those are rare and shared across all tenants.

*Extension path (not in PoC):* allow per-kind templates stored **in the DB** (sandboxed Jinja2),
for fully runtime-defined block types. Deferred because executing DB-stored templates is a
security surface (sandboxing) we don't want to design first.

## 7. Caching & invalidation — the actual hard problem

Rendering is cheap to cache; *invalidating* correctly is the engineering core, because Elements
are shared (a header/footer is reused on every page of a tenant).

**Cache layers**
- **Page output** — full computed HTML per `(tenant, page, variant)`.
- **Fragment output** — per reusable Element / htmx-addressable subtree.

**Dependency graph.** During render, `RenderContext.record_dependency` collects every Element id
touched while producing a page (or fragment). We persist the **reverse index** so invalidation
is a lookup, not a re-render:

- DB-backed (matches "max state in DB", durable, queryable):
  `PageDependency(page_id, element_id)` — unique per pair, rewritten on each render of a page.
- Or Redis tag-sets keyed by element id, for speed. Start DB-backed; add Redis tags if hot.

**Invalidation.** On `Element` `post_save`/`post_delete` (signal), look up dependent pages and
fragments and evict their cached output. Collections extend this to pages: a `page_list`
records its parent page's id in the same dependency set, so saving, moving (the old parent is
captured `pre_save`), or deleting a child page evicts every page and fragment listing it. Tenant-wide elements (header/footer, global settings)
invalidate the whole tenant's page cache. All cache keys are **prefixed by tenant schema** so
eviction and isolation are per-tenant.

This dependency layer should be **built first** — everything downstream assumes it exists.

**Cache backend — Postgres by default, Redis when configured.** In keeping with "max state in
the DB", the default cache backend is Postgres (`DatabaseCache`) over a single `uruk_cache` table
in the **public schema** — one shared store, not a per-tenant table. Tenant isolation is *not*
physical here; it is enforced by the same `tenant_key_function` prefix used for Redis, so cached
output never leaks across tenants regardless of backend. When `URUK_REDIS_URL` is set, Redis
takes over transparently (same key function). Postgres-default means a bare `docker compose up`
of Postgres alone is a complete, multi-worker-safe deployment — Redis is a performance upgrade,
never a prerequisite. (Local/test with no Redis previously used per-process `LocMem`, which is
invisible to other Granian workers and to the invalidation signals they fire; the shared DB table
fixes that.)

## 8. Configuration in the database

A **Setting** model (`key`, `value` JSON, scope) holds tenant configuration. A typed
**settings registry** in code declares each key's default, type, and validation — so DB config
is validated and discoverable rather than free-form.

Avoid the "load every option on every request" trap: namespace keys, **lazy-load** by namespace, and
**cache the resolved settings** per tenant (invalidated on Setting save, like any other element).

## 9. Asset pipeline — JS/CSS (and media) in the DB

`Asset` stores source/bytes plus a **sha256 content hash**. Assets live in the tenant schema, so
they travel with the site archive (§10).

Serving honours the goal of "few files on disk" **without fighting the browser**:
- Served from a view at `/_assets/<sha256>/<name>` with `Cache-Control: public, max-age=31536000, immutable` + `ETag`.
- Content-hashed URLs mean the long cache is safe — a change is a new URL.
- **Critical CSS is inlined** into the computed page; the rest is referenced by hashed URL.
- Only framework static (the Django admin, if used) goes through `collectstatic`.

This keeps source editable in the DB and portable, while still getting edge/browser caching.

### Storage backends — an escape hatch for the bytes only

The bytes-in-`bytea` default is right for JS/CSS and ordinary images, but it hits Postgres'
1 GB-per-value ceiling and its load-the-whole-blob-into-memory serving model. So the byte storage
(and *only* the byte storage) has an env-gated escape hatch, while the `Asset` **row** — hash,
name, content-type, metadata — always stays in the tenant schema:

- `URUK_STORAGE_BACKEND` unset → **bytes live in `Asset.data` (`bytea`)**, exactly as before. This
  is the default and needs nothing but Postgres.
- `URUK_STORAGE_BACKEND=filesystem` → `FileSystemBackend`, blobs under `URUK_STORAGE_ROOT`.
- `URUK_STORAGE_BACKEND=boto` → `BotoStorageBackend`, an S3-compatible bucket (`URUK_STORAGE_*`).

When a backend is active, `Asset.data` is left empty and `Asset.storage_key` names the external
blob; the `sha256` is still computed from the bytes at upload time. **Blobs are content-addressed
by their sha256** — identical content dedups for free and the key is stable, mirroring the
immutable-URL design. An `Asset.read_bytes()` helper hides *where* the bytes are: DB column or
backend. Serving and rendering call `read_bytes()`; they don't know or care which backend answered.

**Portability cost, paid explicitly.** With a backend active, a site archive is no longer a pure
schema dump — the external blobs must travel too. `archive_tenant` therefore always serialises the
*bytes* (via `read_bytes()`), not the storage key, so an archive is self-contained regardless of
the source backend; `restore_tenant` writes those bytes back through the *destination's* active
backend. A site archived from an S3-backed instance restores cleanly onto a Postgres-only one and
vice-versa. The portability guarantee holds; it just isn't free when the blobs live outside the DB.

## 10. UUIDv7 primary keys & site portability

Every model's PK is a **UUIDv7** (time-ordered UUID), via an abstract base:

```python
import uuid
from django.db import models

class UUID7Model(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid7, editable=False)  # py3.14 stdlib
    class Meta:
        abstract = True
```

Two payoffs, both central to the vision:
1. **Performance** — UUIDv7 is time-ordered, so inserts hit the end of the B-tree index
   (good locality), avoiding the random-insert page-split / write-amplification penalty of UUIDv4.
2. **Portability ("fearless" archive/restore)** — globally-unique PKs mean a tenant exported from
   one instance imports into another with **zero PK-collision risk**. Combined with
   schema-per-tenant, **a site archive is essentially a dump of one schema**: export the tenant's
   rows (content, elements, assets, settings, users) and load them into a fresh schema elsewhere.
   The archive/restore API is built on this property.

## 11. MCP server — the first "UI" is an agent

Because administration is API-first, the **first client we build is an MCP server**, not a web
UI. An AI agent (e.g. Claude) becomes the way you build and edit sites — describe the change in
natural language, the agent calls tools, the site updates. This is the fastest path to a usable
system and a forcing function that keeps the admin API complete and clean.

**Layering — no duplicated logic.** The MCP server is a thin adapter. All behaviour lives in a
**service layer** that both django-ninja and MCP call; MCP tools are not a second implementation.
(Equivalently, the MCP server can call the HTTP API in-process — but a shared service layer is
cleaner and keeps everything typed.)

**Authorisation is still Django's.** An MCP connection authenticates as a **Django user with a
token**, scoped to a **tenant**. What the agent can do is exactly that user's Django permissions —
the same model as §4. No parallel permission system. A read-only token yields read-only tools.

**Tenant context.** Each tool call resolves a tenant (configured per-connection, or passed as an
argument), and runs inside that schema via `django-tenants`.

**Tool surface (initial):**
- *Discovery* — `list_pages`, `get_page_tree`, `list_element_kinds` (the renderer
  vocabulary, so agents don't guess kind names), `list_elements`, `get_element`,
  `get_element_tree` (an element's subtree with ids — how the agent reads existing
  structure before editing it), `list_assets`, `get_settings`.
- *Content* — `create_page`, `update_page`, `set_page_root_element`, `delete_page`,
  `create_element`, `update_element`, `move_element` / `reorder_children` (tree ops),
  `delete_element`. (No separate `set_element_content` — `update_element(content=…)` covers it.)
- *Config & assets* — `set_setting`, `upload_asset`.
- *Render preview* — **`render_page` / `render_fragment` returning the computed HTML.** This closes
  the loop: the agent *sees the rendered result* of its edits, mirroring the outside-in test
  philosophy (§12) — change via tool, verify via rendered output.
- *Portability* — `archive_tenant`, `restore_tenant` (§10), gated by platform-admin permission.

**Transport.** Python MCP SDK over stdio for local/dev; streamable HTTP (`mcp_server/http.py`,
stateless, `/mcp`) for hosted agents. Over HTTP every request carries its own bearer token and
the `Host` selects the tenant exactly as for the website — the host is only a routing hint, the
token must resolve inside that tenant's schema. It runs as its own Granian ASGI process (the
compose `mcp` service), so the website stays WSGI. Keep tool
schemas typed (they fall out of the same Pydantic/ninja schemas — one more reason for §3's
type-everything choice).

## 12. Testing strategy — outside-in

Optimise for **refactorability**: test behaviour through the public surface, not internals.

- **Primary suite — API + rendering integration tests.** Drive the admin APIs (POST/PUT/DELETE),
  then assert on:
  1. **DB state** in the tenant schema, and
  2. the **rendered website response** (GET the page, assert the HTML; assert htmx fragment
     responses). Parse HTML with `lxml`/`selectolax` rather than brittle string matching.
- **Few unit tests**, reserved for genuinely tricky pure logic: the dependency-graph computation,
  cache-key construction, settings resolution, UUID/portability helpers.
- **Tenant-isolation tests are mandatory**: data created in tenant A must be invisible to tenant B,
  including under the connection pool (§3 risk #2).
- **`nplusone`** in tests to catch query explosions in the renderer.
- Real Postgres in CI (schemas needed). `factory-boy` for setup. High coverage, but measured on
  the integration suite — coverage that survives refactoring.

## 13. Proposed project structure

```
config/         # settings, urls, wsgi/asgi   (was core/)
tenants/        # SHARED: Tenant, Domain, platform-admin
content/        # TENANT: Page, Element, Asset, Setting models
services/       # shared service layer (called by api/ and mcp/)
rendering/      # registry, element renderers, RenderContext
caching/        # dependency index, invalidation signals, tag layer
api/            # django-ninja routers, schemas, Django-permission auth
mcp_server/     # MCP server: tools over the service layer (named to avoid the `mcp` SDK clash)
tests/          # outside-in integration suite
```

## 14. Phased roadmap

1. **Foundation** — Django 6 / py3.14 project, Ruff (lint+format), `ty`, pytest+Postgres CI,
   `UUID7Model` base. Verify the §3 compatibility risks *first*.
2. **Tenancy** — `django-tenants` wired; `Tenant`/`Domain`; tenant-isolation test under the pool.
3. **Content model** — `Page`, `Element`, `Asset`, `Setting` in the tenant schema.
4. **Rendering** — renderer registry + core block kinds; page renders from the Element tree.
5. **Caching + invalidation** — dependency index, output/fragment cache, signal-based eviction.
   *(The heart — get this right before adding surface area.)*
6. **Service layer + Admin API** — content operations as a typed service layer; django-ninja CRUD
   over it, authorised by Django permissions.
7. **MCP server** — tools over the service layer (incl. `render_page` preview). *This is the first
   client; we build and edit sites through the agent, before any UI.*
8. **Assets** — JS/CSS-in-DB, hashed immutable serving, critical-CSS inlining.
9. **htmx** — dynamic fragment endpoints returning cached fragments.
10. **Portability** — tenant archive/restore API built on schema dump + UUIDv7.
11. **Common-needs pass** — pick the handful of everyday site capabilities worth covering
    (forms, redirects, SEO metadata, menus) and model them as element kinds + settings.
12. **Observability — OpenTelemetry** — `OTEL_*` env-var driven, initialised only when an
    exporter endpoint is configured (zero overhead otherwise); tenant `schema_name` on every
    span. See §3.
13. **(Later) Edge — Caddy in front** — on-demand TLS for tenant custom domains (`ask` endpoint
    backed by `Domain`) + compression; see §3. Never a proxy cache — §7 owns caching.
14. **(Later) Deployment — AWS, free tier first** — target shape: one small EC2 instance
    running Docker Compose with Caddy (§3, item 13) + the single self-provisioning app
    container + on-box Postgres (RDS is the upgrade path, not the start). The minimum
    footprint is deliberate: no Redis (Postgres cache fallback) and assets-in-DB mean
    app + Postgres is the whole system. Backups = §10 portability archives shipped to S3.
    To verify when it lands: ~1 GB RAM tuning (`URUK_WORKERS=1` & the PR #18 knobs, modest
    Postgres `shared_buffers`), which AWS free regime applies (credits-based plan for
    accounts since mid-2025 vs the classic 12-month tier), public-IPv4 billing, image
    registry (ECR/Docker Hub), and DNS for tenant domains.
15. **(Later) Human UI** — only once the agent workflow proves the API surface is complete.
16. **Drafts & revisions** — working copy vs immutable published snapshots, preview, rollback,
    discard (§16). *Done.*

## 15. Open questions

- Per-tenant vs shared **users** — defaulting per-tenant (§4); confirm no near-term need for
  cross-tenant identity.
- **Page variants** — do we cache per-locale / per-device / per-auth-state? Multi-lang is a stated
  long-term goal; decide the cache-key variant axes before §5 ships.
- ~~**Versioning/history**~~ — decided: whole-site snapshots (§16).
- DB-stored sandboxed templates (§6 extension) — if/when runtime-defined block kinds are needed.

## 16. Drafts, publishing & revisions

An agent edits a site in many small steps; visitors must never see the half-done
states in between. So content has two layers:

- **Working copy** — the Element/Page/Setting rows. Every API, MCP, and admin write lands
  here, exactly as before.
- **Revisions** — `publish` freezes the working copy into an immutable `Revision` (a JSON
  snapshot in the §10 record format: elements, pages, settings). The public site serves
  the **latest** revision.

**Whole-site, not per-page.** Elements are shared (one header serves every page), so
per-page publishing would allow incoherent mixes of old and new shared elements. A
revision is the whole site, like a commit; it is cheap because sites are small JSON.

**Live mode vs draft mode.** A site with no revision is in *live mode*: the working copy is
served directly, which suits simple setups that need no review step. The first publish switches it to *draft mode* for good. The seeded default
site and restored archives are published on arrival, so they start in draft mode.

**Rendering from a snapshot.** Renderers read through a `ContentSource` on the
`RenderContext` (`rendering/sources.py`): `DatabaseSource` for the working copy,
`SnapshotSource` (unsaved model instances indexed in memory) for a revision. The same
committed renderers draw both, so the preview is exactly what publishing will put live.
Parsed snapshots are kept per process in a small LRU keyed by `(schema, revision id)`.
Assets are not snapshotted: they are content-addressed and immutable, so a snapshot
refers to them by id.

**Caching becomes trivial for published output.** A revision never changes, so its pages
and fragments are cached under revision-scoped keys (`published:<revision>:…`) with no
invalidation at all — publishing simply moves serving to new keys. The §7 dependency
graph keeps serving the working copy (live mode and the preview), where content does change.

**Surfaces.**
- Public `/<path>` and `/_fragment/<id>` serve the live revision (never the draft).
- `/_preview/<path>` serves the working copy to signed-in users with `content.view_page`
  (session login via the admin at `/<URUK_ADMIN_PATH>/`, default `/uruk/`); `Cache-Control: private, no-store` + `X-Robots-Tag: noindex`.
- API: `GET /publishing` (mode, live revision, diff of unpublished changes),
  `POST /publishing/publish`, `POST /publishing/discard`, `GET /revisions`,
  `POST /revisions/{id}/rollback`. MCP: `get_publishing_status`, `publish`, `list_revisions`,
  `rollback`, `discard_draft`; `render_page` previews the working copy.
- Permissions: `content.add_revision` publishes and rolls back, `content.view_revision`
  reads history and status, the custom `content.discard_draft` resets the working copy.

**History is append-only.** `rollback` republishes an old snapshot as a *new* revision
(`Rollback to #N`) and leaves the working copy alone; `discard_draft` rewrites the working
copy from a revision (default: the live one), preserving ids. Archives carry the revision
history; one without it is published once on restore.

*Later:* scheduled publishing (a `publish_at` on a pending revision), per-revision preview
URLs, and preview links that keep the `/_preview/` prefix when navigating.
