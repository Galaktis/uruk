# Contributing to Uruk

Thanks for your interest in Uruk! Bug reports, ideas, documentation fixes and code are all
welcome. This page explains how to get a working environment and what a pull request needs
to be merged.

## Before you start

- **Read [ARCHITECTURE.md](ARCHITECTURE.md).** It is the authoritative design document:
  data model, rendering, caching, tenancy, MCP and the roadmap. Structural changes should
  fit it — or update it in the same pull request.
- **Open an issue first** for anything larger than a small fix, so we can agree on the
  approach before you invest time in it.
- **Security issues** go through [SECURITY.md](SECURITY.md), never a public issue.

## Development setup

Requirements: Python 3.15, [uv](https://docs.astral.sh/uv/) and Docker (Postgres and
Redis run in containers; there is no SQLite fallback, since `django-tenants` needs schemas).

```bash
git clone https://github.com/Galaktis/uruk && cd uruk
docker compose up -d postgres redis   # backing services only (= just up)
uv sync                               # runtime + dev dependencies
uv run python manage.py migrate       # public + tenant schemas
uv run pytest                         # full suite (needs Postgres up)
```

`uv run python manage.py runserver` starts a local dev server; a bare `docker compose up`
runs the complete stack exactly as CI smoke-tests it.

## Quality gate

CI (`.github/workflows/ci.yml`) runs these on every pull request — run them locally first:

```bash
uv run ruff format .     # format
uv run ruff check .      # lint (ruff "ALL" ruleset, line length 160)
uv run ty check          # type-check
uv run coverage run -m pytest && uv run coverage report   # tests; fails under 95% coverage
```

## Code conventions

- **Every module, class and function has a docstring.** A `# noqa` carries a comment
  explaining why.
- Modules start with `from __future__ import annotations`; type-only imports go under
  `if TYPE_CHECKING:`.
- Content logic lives in `services/` — the API and the MCP server both call it, so never
  duplicate it in a view or a tool.
- Renderers read content only through `ctx.source`, never by querying models directly.
- Everything is type-checked by ty with django-stubs and no suppressions. Reach the
  tenant connection through `common.tenancy` and the request user through `common.users`
  rather than `django.db.connection` / `request.user` directly.
- Cache invalidation (`caching/`) is the engineering core: change it carefully, and cover
  the change with a test.
- New configuration variables are documented in three places kept in sync: the README
  "Configuration" section, the comments in `docker-compose.yml`, and the default site's
  `/configuration` page (`scripts/default_site/configuration.html`).

## Tests

Tests are **outside-in**: drive the service, API or HTTP surface, then assert on the tenant
database state and the rendered HTML. Anything that touches tenancy should also prove
isolation between tenants. Every bug fix comes with a test that fails without it.

## The default website

The site served on a fresh deployment is authored in `scripts/default_site/`. After editing
any of its sources, regenerate the committed archive and include it in your commit:

```bash
uv run python scripts/build_default_site.py   # rewrites tenants/seed/default_site.json
```

## Pull requests

- Keep each pull request focused on one change, with a description of *what* and *why*.
- Write commit messages in the imperative mood ("Add RSS feed for collections").
- Make sure the quality gate passes and update the docs your change affects.

By contributing, you agree that your contributions are licensed under the project's
[BSD 2-Clause License](LICENSE), and to follow the [Code of Conduct](CODE_OF_CONDUCT.md).
