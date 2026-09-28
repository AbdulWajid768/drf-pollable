"""App config -- discovers every declaration style once the registry is ready."""

from __future__ import annotations

from django.apps import AppConfig
from django.utils.module_loading import autodiscover_modules


class DrfPollableConfig(AppConfig):
    name = "drf_pollable"
    verbose_name = "DRF Pollable"
    default_auto_field = "django.db.models.AutoField"

    #: ``ready()`` can run more than once (notably under the test runner);
    #: discovery is idempotent but we skip the repeat work anyway.
    _discovered = False

    def ready(self):
        if self.__class__._discovered:
            return
        self.__class__._discovered = True

        from .registration import registry

        # Order matters, and later sources win:
        #   1. polls.py in each app  -- the common case
        #   2. model PollingMeta     -- declared next to the data
        #   3. settings ENDPOINTS    -- central, so ops can override the above
        autodiscover_modules("polls")
        registry.load_from_models()
        registry.load_from_settings()
