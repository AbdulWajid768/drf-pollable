"""Two-layer configuration for drf-pollable.

Effective value for any option resolves as::

    per-endpoint value  ->  settings.DRF_POLLABLE["DEFAULTS"]  ->  BUILTIN_DEFAULTS

Projects configure globals in ``settings.py``::

    DRF_POLLABLE = {
        "DEFAULTS": {"min_poll_interval": 5, "jitter": 0.1},
        "ENDPOINTS": {
            "order-status": {"source": "shop.Order.status"},
        },
    }
"""

from __future__ import annotations

from typing import Any

from django.conf import settings as django_settings
from django.core.signals import setting_changed
from django.dispatch import receiver

#: Built-in fallbacks. Every option a registration understands appears here so
#: that ``resolve_config()`` always returns a fully-populated mapping and call
#: sites never need ``.get(key, fallback)``.
BUILTIN_DEFAULTS: dict[str, Any] = {
    # --- kill switch -------------------------------------------------------
    "enabled": True,
    # --- interval negotiation (see README "Who decides the interval?") -----
    # Hard floor the server is willing to be polled at.
    "min_poll_interval": 5,
    # What we advertise via Retry-After. ``None`` -> falls back to the floor.
    "suggested_poll_interval": None,
    # Ceiling used when clamping a client-requested interval / backoff growth.
    "max_poll_interval": 3600,
    # When True a too-early poll is rejected with 429 instead of merely hinted.
    "enforce_interval": False,
    # Query param a client may use to request its own cadence. None disables.
    "client_interval_param": "interval",
    # "fixed" | "exponential" -- how the *suggested* interval grows while the
    # value keeps coming back unchanged.
    "backoff": "fixed",
    "backoff_factor": 2.0,
    # Ceiling for backoff growth. ``None`` -> max_poll_interval.
    "backoff_max": None,
    # Random spread applied to the advertised interval, as a fraction (0.1 =
    # +/-10%). Prevents synchronised clients re-polling in lockstep.
    "jitter": 0.0,
    # --- response shape ----------------------------------------------------
    # True  -> {"value": ..., "done": ..., "next_poll_in": ...}
    # False -> the bare serialized value, with the metadata in headers only.
    "envelope": True,
    "send_etag": True,
    "send_last_modified": True,
    # --- performance -------------------------------------------------------
    # Seconds to cache the computed value+version. 0 disables.
    "cache_timeout": 0,
    # Keep each user's cached value separate. Only set False when the data is
    # genuinely identical for every caller -- otherwise one user's value would
    # be served to another.
    "cache_vary_on_user": True,
    # --- hints -------------------------------------------------------------
    # After this many consecutive *changed* polls, add an X-Poll-Upgrade-Hint
    # header suggesting websockets/SSE would suit this data better. 0 disables.
    "upgrade_hint_after": 0,
    # --- DRF plumbing ------------------------------------------------------
    "throttle_scope": None,
    "throttle_classes": None,
    "permission_classes": None,
    "authentication_classes": None,
    # --- lifecycle ---------------------------------------------------------
    # RFC 8594 Sunset header, e.g. "2027-01-01" or a date/datetime.
    "sunset": None,
    "deprecation_message": None,
    # --- routing -----------------------------------------------------------
    "url_prefix": "poll",
    "lookup_field": "pk",
    "lookup_url_kwarg": None,
}

_cache: dict[str, Any] = {}


def _source_keys() -> frozenset[str]:
    """Keys that describe *what* to poll rather than *how*.

    Imported lazily so ``conf`` stays importable before the app registry is up.
    """
    from .sources import SOURCE_ARG_NAMES

    return SOURCE_ARG_NAMES | {"as_list"}


def _raw() -> dict[str, Any]:
    return getattr(django_settings, "DRF_POLLABLE", None) or {}


def global_defaults() -> dict[str, Any]:
    """BUILTIN_DEFAULTS overlaid with ``DRF_POLLABLE["DEFAULTS"]``."""
    if "defaults" not in _cache:
        merged = dict(BUILTIN_DEFAULTS)
        merged.update(_raw().get("DEFAULTS") or {})
        unknown = set(merged) - set(BUILTIN_DEFAULTS)
        if unknown:
            raise ImproperlyConfiguredDefaults(sorted(unknown))
        _cache["defaults"] = merged
    return _cache["defaults"]


def settings_endpoints() -> dict[str, dict[str, Any]]:
    """Endpoints declared centrally via ``DRF_POLLABLE["ENDPOINTS"]``."""
    return dict(_raw().get("ENDPOINTS") or {})


def resolve_config(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Merge per-endpoint ``overrides`` over the global defaults.

    Unknown keys are rejected loudly: a silently-ignored typo in
    ``min_poll_intervall`` would otherwise look like the option simply had no
    effect.
    """
    config = dict(global_defaults())
    source_keys = _source_keys()
    for key, value in (overrides or {}).items():
        if key in source_keys:
            continue
        if key not in BUILTIN_DEFAULTS:
            raise ImproperlyConfiguredDefaults([key])
        config[key] = value

    # Derived fallbacks, applied once so the view layer can read them directly.
    if config["suggested_poll_interval"] is None:
        config["suggested_poll_interval"] = config["min_poll_interval"]
    if config["backoff_max"] is None:
        config["backoff_max"] = config["max_poll_interval"]
    return config


class ImproperlyConfiguredDefaults(Exception):
    """Raised when DRF_POLLABLE contains options drf-pollable does not know."""

    def __init__(self, keys: list[str]):
        known = ", ".join(sorted(BUILTIN_DEFAULTS))
        super().__init__(
            f"Unknown drf-pollable option(s): {', '.join(keys)}. Valid options are: {known}"
        )


@receiver(setting_changed)
def _reset_cache(sender, setting, **kwargs):  # pragma: no cover - test plumbing
    if setting == "DRF_POLLABLE":
        _cache.clear()
