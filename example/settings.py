"""Minimal Django project demonstrating drf-pollable."""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

SECRET_KEY = "example-only-do-not-use-in-production"
DEBUG = True
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "rest_framework",
    "drf_pollable",
    "shop",
]

MIDDLEWARE = ["django.middleware.common.CommonMiddleware"]

ROOT_URLCONF = "urls"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    }
}

USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.AutoField"

REST_FRAMEWORK = {
    "DEFAULT_THROTTLE_RATES": {
        "orders": "30/min",
    }
}

# Global polling defaults -- every endpoint inherits these unless it overrides.
DRF_POLLABLE = {
    "DEFAULTS": {
        "min_poll_interval": 3,
        "jitter": 0.1,
    },
}
