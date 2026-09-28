"""URL generation for registered polling endpoints.

Drop one line in your root ``urls.py`` and every registered endpoint is routed::

    from drf_pollable import poll_urls

    urlpatterns = [
        path("api/", include(poll_urls())),
    ]

which yields, for an endpoint named ``order-status``::

    /api/poll/order-status/<pk>/
"""

from __future__ import annotations

from django.urls import include, path

from .registration import PollRegistry
from .registration import registry as default_registry


class PollRouter:
    """Builds URL patterns from a registry."""

    def __init__(self, registry: PollRegistry | None = None, prefix: str | None = None):
        self.registry = registry if registry is not None else default_registry
        #: Overrides each endpoint's own ``url_prefix`` when given.
        self.prefix = prefix

    @property
    def urls(self) -> list:
        """One ``path()`` per registered endpoint, grouped by prefix."""
        patterns = []
        for registration in self.registry:
            prefix = self.prefix if self.prefix is not None else registration.config["url_prefix"]
            path_suffix = registration.url_path
            route = f"{prefix.strip('/')}/{path_suffix}" if prefix else path_suffix
            patterns.append(
                path(route, registration.view.as_view(), name=registration.url_name)
            )
        return patterns

    def __iter__(self):
        return iter(self.urls)


def poll_urls(registry: PollRegistry | None = None, prefix: str | None = None):
    """Return an ``include()``-able URLconf for every registered endpoint.

    ``urlpatterns`` is rebuilt on each call, so this must be called *after*
    registrations have loaded -- i.e. from your URLconf, not at module import
    in an app that loads before the ones declaring endpoints.
    """
    router = PollRouter(registry=registry, prefix=prefix)
    return include((router.urls, "drf_pollable"), namespace="poll")
