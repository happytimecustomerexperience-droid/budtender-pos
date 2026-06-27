"""Django settings — single-operator budtender app. Env-driven, lean.

App data (sessions, audit, cached customers) lives in a local sqlite by default.
Customer-360 history reads the marketing-dashboard's Postgres `_log` tables READ-ONLY
via the optional `dashboard` DB alias (DASHBOARD_DB_DSN) — we never re-ingest.
"""

import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# Load .env (all secrets live there; nothing hardcoded). Skipped under pytest
# (order-independent) so a local .env can't leak into tests. No-op if dotenv absent.
if "pytest" not in sys.modules and not os.environ.get("BUDTENDER_TESTING"):
    try:
        from dotenv import load_dotenv

        load_dotenv(BASE_DIR / ".env")
    except Exception:
        pass


def _env_bool(key, default="0"):
    return (os.environ.get(key, default) or "").lower() in ("1", "true", "yes", "on")


SECRET_KEY = os.environ.get("BUDTENDER_SECRET_KEY", "dev-insecure-change-me")
DEBUG = _env_bool("BUDTENDER_DEBUG", "1")
ALLOWED_HOSTS = [h.strip() for h in
                 os.environ.get("BUDTENDER_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]
# Django 4+ needs scheme-qualified CSRF origins for the prod domain (https://host).
CSRF_TRUSTED_ORIGINS = [o.strip() for o in
                        os.environ.get("BUDTENDER_CSRF_ORIGINS", "").split(",") if o.strip()]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "customers",
    "budtender",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",  # serve static at scale w/o a CDN
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "budtender_pos.urls"

TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "templates"],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]

WSGI_APPLICATION = "budtender_pos.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        # Persist on a mounted volume in Docker via BUDTENDER_DB_PATH.
        "NAME": os.environ.get("BUDTENDER_DB_PATH", str(BASE_DIR / "db.sqlite3")),
    }
}
# Optional read-only mirror of the dashboard's Postgres for customer history.
_dash = os.environ.get("DASHBOARD_DB_DSN", "")
if _dash:
    import urllib.parse as _u

    p = _u.urlparse(_dash)
    DATABASES["dashboard"] = {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": p.path.lstrip("/"),
        "USER": p.username or "",
        "PASSWORD": p.password or "",
        "HOST": p.hostname or "",
        "PORT": str(p.port or 5432),
        "OPTIONS": {"options": "-c default_transaction_read_only=on"},
    }
DASHBOARD_TENANT_SCHEMA = os.environ.get("DASHBOARD_TENANT_SCHEMA", "")

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = os.environ.get("BUDTENDER_TZ", "America/Los_Angeles")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "/login/"

# Cache — LocMem by default (fine single-process); set REDIS_URL for multi-worker.
_redis = os.environ.get("REDIS_URL", "")
if _redis:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.redis.RedisCache",
                          "LOCATION": _redis}}
else:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

# Security — hardened by default; relax only in DEBUG.
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = False
X_FRAME_OPTIONS = "DENY"
if not DEBUG:
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Upload caps (id-scan guard)
DATA_UPLOAD_MAX_MEMORY_SIZE = 8 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 8 * 1024 * 1024
ID_SCAN_MAX_IMAGE_BYTES = 6 * 1024 * 1024

LOGGING = {
    "version": 1, "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}
