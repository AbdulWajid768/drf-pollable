"""Settings for the drf-pollable test suite."""

SECRET_KEY = "drf-pollable-tests"
DEBUG = False
USE_TZ = True

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.sessions",
    "rest_framework",
    "drf_pollable",
    "tests.testapp",
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
]

ROOT_URLCONF = "tests.urls"

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "drf-pollable-tests",
    }
}

REST_FRAMEWORK = {
    "UNAUTHENTICATED_USER": "django.contrib.auth.models.AnonymousUser",
    "DEFAULT_THROTTLE_RATES": {
        "tight": "2/min",
    },
}

# Declaration style 4: endpoints declared centrally in settings.
DRF_POLLABLE = {
    # Deliberately not the built-in default (5), so tests can prove that the
    # global layer actually overrides the package defaults.
    "DEFAULTS": {
        "min_poll_interval": 7,
    },
    "ENDPOINTS": {
        "ticket-from-settings": {
            "source": "testapp.Ticket.state",
            "min_poll_interval": 11,
            "done_when": "closed",
        },
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.AutoField"
