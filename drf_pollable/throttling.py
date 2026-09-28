"""Polling-aware throttles.

These layer on top of DRF's own throttling rather than replacing it, so a
project keeps using ``DEFAULT_THROTTLE_RATES`` and its existing conventions:

============================  ================================================
``PollIntervalThrottle``      per-object cadence floor (the ``min_poll_interval``
                              guard) -- stops a client hammering *one* record
``PollScopedRateThrottle``    per-endpoint budget via ``throttle_scope``
``PollBurstThrottle``         short bursts, e.g. ``"10/sec"``
``PollSustainedThrottle``     sustained ceiling, e.g. ``"600/hour"``
``TenantRateThrottle``        per-tenant / per-API-key isolation
============================  ================================================
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ImproperlyConfigured
from rest_framework.throttling import (
    BaseThrottle,
    ScopedRateThrottle,
    SimpleRateThrottle,
    UserRateThrottle,
)

from .state import default_store, state_key


class PollIntervalThrottle(BaseThrottle):
    """Enforce an endpoint's ``min_poll_interval`` per client *per object*.

    This is deliberately narrower than a rate limit: a client may be well within
    its overall budget yet still be polling one record ten times a second. It
    only engages when the endpoint sets ``enforce_interval=True``; otherwise the
    floor is advertised via ``Retry-After`` but not policed.
    """

    def __init__(self):
        self._wait = None

    def allow_request(self, request, view) -> bool:
        config = getattr(view, "poll_config", None)
        if not config or not config.get("enforce_interval"):
            return True

        registration = getattr(view, "registration", None)
        if registration is None:  # pragma: no cover - defensive
            return True

        state = default_store.get(state_key(registration.name, request, _object_id(view)))
        elapsed = state.seconds_since_last_poll()
        if elapsed is None:
            return True

        floor = float(config["min_poll_interval"])
        if elapsed < floor:
            self._wait = max(1, int(round(floor - elapsed)))
            return False
        return True

    def wait(self):
        return self._wait


class PollScopedRateThrottle(ScopedRateThrottle):
    """``ScopedRateThrottle`` that is a no-op when no scope is configured."""

    def allow_request(self, request, view):
        if not getattr(view, self.scope_attr, None):
            return True
        return super().allow_request(request, view)


class PollBurstThrottle(UserRateThrottle):
    """Short-window ceiling. Configure ``DEFAULT_THROTTLE_RATES["poll_burst"]``."""

    scope = "poll_burst"

    def allow_request(self, request, view):
        if not self.rate:
            return True
        return super().allow_request(request, view)


class PollSustainedThrottle(UserRateThrottle):
    """Long-window ceiling. Configure ``DEFAULT_THROTTLE_RATES["poll_sustained"]``."""

    scope = "poll_sustained"

    def allow_request(self, request, view):
        if not self.rate:
            return True
        return super().allow_request(request, view)


class TenantRateThrottle(SimpleRateThrottle):
    """Rate-limit per tenant so one customer cannot starve the others.

    Point ``DRF_POLLABLE["TENANT_RESOLVER"]`` at a dotted path to a callable
    that takes the request and returns a tenant identifier (or ``None`` to skip
    throttling for that request)::

        DRF_POLLABLE = {"TENANT_RESOLVER": "myapp.tenancy.current_tenant_id"}
        REST_FRAMEWORK = {"DEFAULT_THROTTLE_RATES": {"poll_tenant": "1000/hour"}}
    """

    scope = "poll_tenant"

    def get_cache_key(self, request, view):
        tenant = self._resolve_tenant(request)
        if tenant is None:
            return None
        return self.cache_format % {"scope": self.scope, "ident": tenant}

    def allow_request(self, request, view):
        if not self.rate:
            return True
        return super().allow_request(request, view)

    @staticmethod
    def _resolve_tenant(request) -> Any:
        from django.conf import settings
        from django.utils.module_loading import import_string

        path = (getattr(settings, "DRF_POLLABLE", None) or {}).get("TENANT_RESOLVER")
        if not path:
            return None
        try:
            resolver = import_string(path) if isinstance(path, str) else path
        except ImportError as exc:  # pragma: no cover - configuration error
            raise ImproperlyConfigured(f"Could not import TENANT_RESOLVER {path!r}: {exc}") from exc
        return resolver(request)


class GlobalPollThrottle(SimpleRateThrottle):
    """A ceiling on *all* traffic to an endpoint, regardless of caller.

    Protects the database when an endpoint is hot across many users. Configure
    ``DEFAULT_THROTTLE_RATES["poll_global"]``.
    """

    scope = "poll_global"

    def get_cache_key(self, request, view):
        registration = getattr(view, "registration", None)
        name = registration.name if registration else "unknown"
        return self.cache_format % {"scope": self.scope, "ident": name}

    def allow_request(self, request, view):
        if not self.rate:
            return True
        return super().allow_request(request, view)


def _object_id(view):
    """The object segment of the URL, when the endpoint has one."""
    registration = getattr(view, "registration", None)
    if registration is None or not registration.source.requires_object:
        return None
    lookup = getattr(registration.source, "lookup_url_kwarg", "pk")
    return (view.kwargs or {}).get(lookup)


#: Applied to every generated polling view. ``PollIntervalThrottle`` and
#: ``PollScopedRateThrottle`` both no-op unless configured, so this default is
#: free for projects that do not opt in.
DEFAULT_POLL_THROTTLES: tuple[type[BaseThrottle], ...] = (
    PollIntervalThrottle,
    PollScopedRateThrottle,
)
