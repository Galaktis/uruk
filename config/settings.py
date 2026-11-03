"""Uruk Django settings.

Configuration is environment-driven via ``URUK_*`` variables; ``docker-compose.yml``
is the reference for the full set.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(name: str, *, default: bool = False) -> bool:
    """Read a boolean environment variable (``"True"`` is true, anything else false)."""
    return os.environ.get(name, str(default)) == "True"


# Core ------------------------------------------------------------------------
DEBUG = env_bool("URUK_DEBUG")
ENVIRONMENT = os.environ.get("URUK_ENV", "unknown")
# The fallback is for local development only; >= 32 bytes so HS256 JWT signing does not warn.
SECRET_KEY = os.environ.get("URUK_SECRET_KEY", "insecure-dev-key-change-me-0123456789")
ALLOWED_HOSTS = os.environ.get("URUK_ALLOWED_HOSTS", "*").split(",")

# Behind a TLS-terminating proxy (Cloudflare Tunnel, Caddy, a load balancer) the app
# sees plain HTTP. URUK_BEHIND_PROXY trusts the proxy's X-Forwarded-Proto so Django
# knows the request was HTTPS — without it, CSRF's Origin check rejects every admin
# and Studio form — and marks the session/CSRF cookies Secure. Enable it only when
# the app is reachable through that proxy alone, since the header is client-settable.
URUK_BEHIND_PROXY = env_bool("URUK_BEHIND_PROXY")
if URUK_BEHIND_PROXY:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
# URL prefix of the Django admin (and the login page the Studio and preview send people
# to). Off the well-known /admin/ by default, which keeps drive-by scanners away; it
# shadows any page with the same top-level slug.
URUK_ADMIN_PATH = os.environ.get("URUK_ADMIN_PATH", "uruk").strip("/") or "uruk"
LOGIN_URL = "admin:login"
# Extra origins (scheme://host) allowed to POST forms, e.g. when the proxy rewrites Host.
CSRF_TRUSTED_ORIGINS = [origin for origin in os.environ.get("URUK_CSRF_TRUSTED_ORIGINS", "").split(",") if origin]

# Bootstrap / autonomy --------------------------------------------------------
# The default tenant answers every host with no more-specific Domain match (see
# tenants.middleware.DefaultTenantFallbackMiddleware and tenants.default); its schema
# name is URUK_DEFAULT_TENANT_SCHEMA, owned by tenants.default. When URUK_ADMIN_EMAIL is
# set, it is the only address permitted to claim a tenant's first superuser via
# POST /api/bootstrap; either way the claim also needs the tenant's claim code
# (printed by `manage.py bootstrap` / `manage.py claim_code`).
URUK_ADMIN_EMAIL = os.environ.get("URUK_ADMIN_EMAIL", "")

# JWT (interactive/human API auth) --------------------------------------------
# Access + refresh tokens are signed HS256 with SECRET_KEY — no separate key
# material. MCP/machine clients keep the long-lived ``uruk_`` API token instead.
URUK_JWT_ACCESS_TTL = int(os.environ.get("URUK_JWT_ACCESS_TTL", "900"))  # 15 minutes
URUK_JWT_REFRESH_TTL = int(os.environ.get("URUK_JWT_REFRESH_TTL", "1209600"))  # 14 days

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Applications ----------------------------------------------------------------
# SHARED_APPS live in the public schema (tenant routing + cross-tenant infra).
# TENANT_APPS are migrated into every tenant's own schema (per-tenant users,
# sessions, and — later — the CMS content models).
SHARED_APPS = [
    "django_tenants",
    "common",
    "tenants",
    "caching",
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.admin",
]
TENANT_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.admin",
    "content",
    "studio",
]
INSTALLED_APPS = [*SHARED_APPS, *[app for app in TENANT_APPS if app not in SHARED_APPS]]

MIDDLEWARE = [
    # Subclass of django-tenants' TenantMainMiddleware: an unmatched Host falls
    # through to the auto-created default tenant instead of 404-ing (tenants.default).
    "tenants.middleware.DefaultTenantFallbackMiddleware",
    "django.middleware.security.SecurityMiddleware",
    # WhiteNoise serves collected admin/static files under Granian in production
    # (runserver only serves them when DEBUG=True). Site content itself is DB data
    # served by content.views — this is for Django's own static assets only.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# Database --------------------------------------------------------------------
# psycopg3 via the django-tenants backend, with Django 6 native connection pooling
# (no external pooler needed).
db_options: dict[str, object] = {}
if env_bool("URUK_DB_POOL", default=True):
    db_options["pool"] = {"min_size": 2, "max_size": 10}

DATABASES = {
    "default": {
        "ENGINE": "django_tenants.postgresql_backend",
        "HOST": os.environ.get("URUK_DB_HOST", "127.0.0.1"),
        "PORT": os.environ.get("URUK_DB_PORT", "5433"),
        "NAME": os.environ.get("URUK_DB_NAME", "uruk"),
        "USER": os.environ.get("URUK_DB_USER", "uruk"),
        "PASSWORD": os.environ.get("URUK_DB_PASSWORD", "uruk"),
        "OPTIONS": db_options,
    },
}
DATABASE_ROUTERS = ("django_tenants.routers.TenantSyncRouter",)
TENANT_MODEL = "tenants.Tenant"
TENANT_DOMAIN_MODEL = "tenants.Domain"

# Cache -----------------------------------------------------------------------
# Computed pages/fragments are cached here; the KEY_FUNCTION prefixes every key
# with the active tenant schema so cached output never leaks across tenants (§7).
# Default backend is Postgres (DatabaseCache) over one shared table in the public
# schema — multi-worker-safe with nothing but Postgres running. Redis takes over
# transparently when URUK_REDIS_URL is set (same key function).
_cache_key_function = "caching.keys.tenant_key_function"
CACHE_TABLE = "uruk_cache"
REDIS_URL = os.environ.get("URUK_REDIS_URL")
if REDIS_URL:
    CACHES = {
        "default": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": REDIS_URL,
            "KEY_FUNCTION": _cache_key_function,
        },
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.db.DatabaseCache",
            "LOCATION": CACHE_TABLE,
            "KEY_FUNCTION": _cache_key_function,
        },
    }

# Asset storage backend -------------------------------------------------------
# By default asset bytes live in Postgres (Asset.data). Setting URUK_STORAGE_BACKEND
# to "filesystem" or "boto" moves the bytes out (the Asset row still lives in the
# tenant schema); see services.storage and ARCHITECTURE.md §9.
URUK_STORAGE_BACKEND = os.environ.get("URUK_STORAGE_BACKEND", "")
URUK_STORAGE_ROOT = os.environ.get("URUK_STORAGE_ROOT", "")
URUK_STORAGE_BUCKET = os.environ.get("URUK_STORAGE_BUCKET", "")
URUK_STORAGE_PREFIX = os.environ.get("URUK_STORAGE_PREFIX", "")
URUK_STORAGE_ENDPOINT_URL = os.environ.get("URUK_STORAGE_ENDPOINT_URL", "")
URUK_STORAGE_REGION = os.environ.get("URUK_STORAGE_REGION", "")

# Internationalisation --------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# Static ----------------------------------------------------------------------
# Django's own static files (the admin) — tenant sites serve their assets from the
# DB (§9), so this is deployment-wide, not per-tenant. In production `collectstatic`
# runs in docker-entrypoint.sh and WhiteNoise serves the result with far-future
# caching (the manifest storage hashes filenames; with DEBUG=True Django skips the
# manifest and runserver serves straight from the app dirs).
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
