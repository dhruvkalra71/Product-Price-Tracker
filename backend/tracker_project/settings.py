import os
import sys
from pathlib import Path
import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent

# Add workspace root to sys.path so backend can import scraper
WORKSPACE_ROOT = str(BASE_DIR.parent)
if WORKSPACE_ROOT not in sys.path:
    sys.path.insert(0, WORKSPACE_ROOT)

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "insecure-dev-key-change-in-prod-ine-price-tracker-2026")

DEBUG = os.getenv("DJANGO_DEBUG", "True").lower() in ("true", "1", "yes")

ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.staticfiles",
    "corsheaders",
    "rest_framework",
    "tracker",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "tracker_project.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
            ],
        },
    },
]

WSGI_APPLICATION = "tracker_project.wsgi.application"

# Database: Supabase / Postgres via DATABASE_URL (Session Pooler on port 5432), fallback to local SQLite
DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=int(os.getenv("CONN_MAX_AGE", "0")),
        ssl_require=bool(os.getenv("DATABASE_URL")),
    )
}

AUTH_PASSWORD_VALIDATORS = []

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# CORS configuration: Credentials not needed for AllowAny public endpoints; disabled to prevent CSRF exposure
CORS_ALLOW_ALL_ORIGINS = True
CORS_ALLOW_CREDENTIALS = False

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "UNAUTHENTICATED_USER": None,
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.AllowAny",
    ],
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
}

# Shared secret for cron authentication (fails closed if unset in production)
SCRAPE_SHARED_SECRET = os.getenv("SCRAPE_SHARED_SECRET", "")

# Concurrency limit for background automated scraping (default: 1 sequential for safe 512MB RAM usage)
SCRAPE_MAX_CONCURRENT = int(os.getenv("SCRAPE_MAX_CONCURRENT", "1"))

# Startup check: Warn if DEBUG=True in production-like environments with DATABASE_URL set
if DEBUG and os.getenv("DATABASE_URL"):
    import logging
    logging.getLogger("django.security").warning(
        "SECURITY WARNING: DEBUG=True is active while DATABASE_URL is configured. "
        "Ensure DJANGO_DEBUG=False is set in production environments."
    )
