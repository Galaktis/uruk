set quiet
set dotenv-load
set positional-arguments

# Launch the backing services (Postgres + Redis) in the background
up *apps="postgres redis":
    docker compose up -d {{apps}}

# Turn off the backing services
down:
    docker compose down

# Install/refresh runtime + dev dependencies
sync:
    uv sync

# Update Dependencies
update:
    uv lock --upgrade

# Update one or more Dependencies
update_package *args='':
    uv lock --upgrade-package $@

# Show Dependencies Tree
tree:
    uv tree

# Launch Python
python *args='':
    uv run python $@

# Run Django Manage Command
manage *args='':
    just python manage.py $@

# Open a Django shell
shell:
    just manage shell

# Make migrations
makemigrations *args='':
    just manage makemigrations $@

# Migrate (public + tenant schemas)
migrate *args='':
    just manage migrate $@

# Make migrations and migrate
migrations:
    just makemigrations
    just migrate

# Migrate + seed the default tenant (the single pre-start step, see docker-entrypoint.sh)
bootstrap:
    just manage bootstrap

# Drop ALL tenants and their schemas, then recreate the seeded default (destructive!)
reset_tenants:
    just manage reset_tenants --yes

# Starts the dev webserver
run:
    just manage runserver

# Collect Django's static files (the admin) into staticfiles/ for WhiteNoise
collectstatic:
    just manage collectstatic --noinput

# Starts the Backend with Granian (production serving; run collectstatic first)
granian:
    uv run granian --interface wsgi config.wsgi:application

# Run the test suite
test *args='':
    uv run pytest $@

# Run tests while storing coverage results
coverage *args='':
    uv run coverage run -m pytest $@
    uv run coverage report

# Run the TY Type Checker
ty:
    uv run ty check

# Run the Ruff Linter and Formatter without fixing anything
lint:
    uv run ruff format --check .
    uv run ruff check --output-format concise .
    uv run ty check

# Run the Ruff Linter and Formatter with fixing and formatting
lint_fix:
    uv run ruff format .
    uv run ruff check . --fix
    uv run ty check

# Run the Ruff Formatter
format:
    uv run ruff format .

# Rebuild tenants/seed/default_site.json from scripts/default_site/
seed:
    just python scripts/build_default_site.py

# Issue a per-tenant MCP API token (prints it once)
token schema username:
    just manage create_api_token {{schema}} {{username}}

# Run the MCP server (reads URUK_MCP_TENANT / URUK_MCP_TOKEN from the env or .env)
mcp:
    just python -m mcp_server

# Run the remote MCP endpoint over HTTP (POST http://localhost:8001/mcp, bearer token auth)
mcp_http:
    uv run granian --interface asgi mcp_server.http:app --port 8001

# Build the demo blog in the running compose stack, as a remote MCP agent (add --reset to rebuild)
demo *args='':
    docker compose exec app uv run python manage.py demo --mcp-url http://mcp:8001/mcp $@

# Init dev environment: services, deps, migrated + seeded default site, admin user
init-dev:
    just up --wait postgres redis
    just sync
    just bootstrap
    just manage tenant_command createsuperuser --schema=default
